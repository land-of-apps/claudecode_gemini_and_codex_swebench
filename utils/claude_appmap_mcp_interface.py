"""
Claude Code backend with AppMap recording — MCP variant.

Architecture (host/container split):
  HOST:
    - claude (subprocess) — same as vanilla `claude` backend
    - `node packages/cli/built/cli.js index --watch -d <clone>` (Popen)
    - `node packages/cli/built/cli.js query mcp` — spawned by claude via .mcp.json
    - skills cloned from getappmap/skills at a pinned SHA

  CONTAINER (per-instance, sweb.eval.x86_64.<id>, pre-built):
    - The project's test suite, run under `appmap-python`, writing
      `.appmap.json` files to /repo/tmp/appmap/pytest/ on a bind mount.
    - The agent never invokes podman directly — `bin/record-appmap.sh`
      in the clone wraps the call.

The host's AppMap index DB is keyed by directory (~/.appmap/data/<sha>/...).
The watcher (cwd=clone, -d=clone) and the MCP server claude spawns (inherits
claude's cwd=clone) therefore land on the same SHA. New `.appmap.json` files
appear via the bind mount, the watcher indexes them, the MCP serves them.

NOTE: We invoke `node <cli.js>` directly rather than the prebuilt macos-arm64
binary at ~/bin/appmap. The binary's `query mcp` is stale.
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import textwrap
import uuid
from pathlib import Path
from typing import Dict, Optional

from utils import claude_session


APPMAP_CLI_JS = os.environ.get(
    "APPMAP_CLI_JS",
    "/Users/kgilpin/source/appland/appmap-js/packages/cli/built/cli.js",
)
SKILLS_DIR = os.environ.get(
    "APPMAP_SKILLS_DIR",
    str(Path.home() / ".claude" / "skills"),
)
# TODO: pin to a SHA from getappmap/skills. For now we trust whatever's installed.

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_TEMPLATE_PATH = REPO_ROOT / "prompts" / "appmap_fix_prompt.txt"


class ClaudeAppMapMcpInterface:
    """Claude on host, recording sandbox in podman."""

    def __init__(self):
        self._check_cli("claude")
        self._check_cli("node")
        self._check_cli("podman")
        if not Path(APPMAP_CLI_JS).is_file():
            raise RuntimeError(f"appmap cli.js not found: {APPMAP_CLI_JS}")
        if not Path(SKILLS_DIR).is_dir():
            raise RuntimeError(f"claude skills dir not found: {SKILLS_DIR}")
        self._prompt = PROMPT_TEMPLATE_PATH.read_text()

    @staticmethod
    def _check_cli(cmd: str) -> None:
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"{cmd} not usable: {r.stderr.strip()}")
        except FileNotFoundError:
            raise RuntimeError(f"{cmd} not found in PATH")

    # ---- public entrypoints ---------------------------------------------

    def prepare_workspace(self, clone: Path, instance: Dict) -> None:
        """Pull the instance image, write all backend-specific scaffolding
        files into the clone, and seed the AppMap index. Does NOT commit
        anything to git — the v2 caller (run_synth) does that itself
        once it has both the bug-applied source and our scaffolding in
        the working tree.

        After this returns, the clone has:
          appmap.yml, .mcp.json, issue.md, CLAUDE.md, tmp/appmap/,
          bin/{record-appmap,run-tests}.sh, .gitignore
        ...but no commits — the dir may not even be a git repo yet.
        """
        self._ensure_instance_image(instance)
        self._write_workspace_files(clone, instance)
        self._index_sha = self._seed_index(clone)

    def run_claude(self, clone: Path, model: Optional[str],
                    instance: Dict) -> Dict[str, object]:
        """Run claude against an already-prepared clone. Manages AppMap
        watcher lifecycle, session live-log mirroring, and post-run
        archive/cleanup. Returns the standard {success, stdout, stderr,
        returncode} dict.
        """
        instance_id = instance.get("instance_id", "unknown")
        run_dir = claude_session.resolve_run_dir(clone, instance_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        session_id = str(uuid.uuid4())
        live_stop = claude_session.start_live_log(session_id, run_dir)
        watcher = self._start_watcher(clone)
        try:
            cmd = ["claude",
                   "--mcp-config", str(clone / ".mcp.json"),
                   "--strict-mcp-config",
                   "--session-id", session_id,
                   "--dangerously-skip-permissions"]
            if model:
                cmd += ["--model", model]
            result = subprocess.run(
                cmd,
                input=self._prompt,
                cwd=str(clone),
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("CLAUDE_APPMAP_TIMEOUT", "1800")),
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return _fail("claude timed out")
        except Exception as e:
            return _fail(str(e))
        finally:
            self._stop_watcher(watcher)
            live_stop.set()
            try:
                self._archive_appmap_data(clone, instance_id, run_dir)
            except Exception as e:
                print(f"  warning: failed to archive appmap data: {e}", flush=True)
            try:
                claude_session.archive_session_logs(clone, run_dir)
            except Exception as e:
                print(f"  warning: failed to archive session logs: {e}", flush=True)
            try:
                claude_session.compute_and_save_usage(run_dir, instance_id)
            except Exception as e:
                print(f"  warning: failed to compute usage stats: {e}", flush=True)
            try:
                self._wipe_appmap_data(clone)
            except Exception as e:
                print(f"  warning: failed to wipe appmap data: {e}", flush=True)

    def execute_code_cli(
        self,
        prompt: str,                         # ignored: this backend builds its own
        cwd: str,                            # host clone dir
        model: Optional[str] = None,
        instance: Optional[Dict] = None,
    ) -> Dict[str, object]:
        """Legacy entry point (v1 fixture flow): prepare workspace, commit
        scaffolding to git, then run claude. New v2 flow (run_synth +
        clean_upstream) calls `prepare_workspace` and `run_claude`
        directly so it can git-init the whole tree at once.
        """
        if instance is None:
            return _fail("claude-appmap requires the SWE-bench instance dict")

        clone = Path(cwd).resolve()
        # podman on macOS only shares specific host dirs (typically /Users) into
        # its VM. /var/folders (the default tempfile.gettempdir() result) is NOT
        # shared, so bind mounts of paths there silently fail with statfs errors.
        if not str(clone).startswith(str(Path.home())):
            return _fail(
                f"clone {clone} is outside $HOME — podman cannot bind-mount it. "
                "Set TMPDIR or SMOKE_CLONE_DIR to a path under your home directory."
            )

        self._index_sha: Optional[str] = None
        try:
            self.prepare_workspace(clone, instance)
            self._commit_scaffolding(clone)
        except Exception as e:
            return _fail(f"setup failed: {e}")

        return self.run_claude(clone, model, instance)

    # ---- per-instance setup ---------------------------------------------

    def _ensure_instance_image(self, instance: Dict) -> None:
        """Pull swebench/sweb.eval.<arch>.<id> if absent. SWE-bench publishes
        prebuilt images to docker hub which is dramatically faster than building
        the base+env+instance chain locally."""
        from swebench.harness.test_spec.test_spec import make_test_spec
        import docker

        if not os.environ.get("DOCKER_HOST"):
            sock = self._discover_podman_socket()
            if sock:
                os.environ["DOCKER_HOST"] = sock

        client = docker.from_env()
        spec = make_test_spec(instance, namespace="swebench")
        self._test_spec = spec
        image_ref = spec.instance_image_key  # e.g. swebench/sweb.eval.x86_64.django_1776_...
        # Podman tags pulled images with the registry prefix; check both forms.
        candidates = [image_ref, f"docker.io/{image_ref}"]
        for ref in candidates:
            try:
                client.images.get(ref)
                print(f"  image present: {ref}", flush=True)
                return
            except docker.errors.ImageNotFound:
                continue
        print(f"  pulling {image_ref} (~3GB; takes several minutes)...", flush=True)
        repo, _, tag = image_ref.partition(":")
        client.images.pull(repo, tag=tag or "latest", platform="linux/amd64")

    @staticmethod
    def _discover_podman_socket() -> Optional[str]:
        try:
            r = subprocess.run(
                ["podman", "machine", "inspect"],
                capture_output=True, text=True, check=True,
            )
            data = json.loads(r.stdout)
            for m in data:
                p = m.get("ConnectionInfo", {}).get("PodmanSocket", {}).get("Path")
                if p:
                    return f"unix://{p}"
        except Exception:
            return None
        return None

    def _write_workspace_files(self, clone: Path, instance: Dict) -> None:
        """Write scaffolding files into clone. Does NOT commit them.

        The legacy entry point (`execute_code_cli` against a v1 fixture)
        finishes the scaffolding by calling `_commit_scaffolding`. The
        v2 entry point (run_synth + clean_upstream + bug_patch) lets
        run_synth git-init the whole tree at once after this returns.
        """
        (clone / "appmap.yml").write_text(self._appmap_yml(instance))
        (clone / ".mcp.json").write_text(self._mcp_json())
        (clone / "issue.md").write_text(self._issue_md(instance))
        (clone / "CLAUDE.md").write_text(self._claude_md())
        (clone / "tmp" / "appmap").mkdir(parents=True, exist_ok=True)
        bin_dir = clone / "bin"
        bin_dir.mkdir(exist_ok=True)
        rec = bin_dir / "record-appmap.sh"
        rec.write_text(self._record_script(instance))
        rec.chmod(0o755)
        runt = bin_dir / "run-tests.sh"
        runt.write_text(self._run_tests_script(instance))
        runt.chmod(0o755)
        self._gitignore_appmap_artifacts(clone)

    def _run_tests_script(self, instance: Dict) -> str:
        """Same container shape as record-appmap.sh, no appmap-python wrap."""
        image = self._test_spec.instance_image_key
        inner = textwrap.dedent("""\
            set -e
            source /opt/miniconda3/bin/activate testbed
            python -m pip install -q -e . >/dev/null 2>&1 || true
            cd /testbed
            exec "$@"
            """)
        return textwrap.dedent(f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ $# -eq 0 ]]; then
              echo "usage: $0 <test-command...>" >&2
              exit 2
            fi
            CLONE="$(cd "$(dirname "$0")/.." && pwd)"
            exec podman run --rm \\
                -v "$CLONE":/testbed -w /testbed \\
                {shlex.quote(image)} \\
                bash -lc {shlex.quote(inner)} _wrap "$@"
            """)

    @staticmethod
    def _issue_md(instance: Dict) -> str:
        """Mirror every input the vanilla SWE-bench PromptFormatter feeds to
        claude: instance_id, repo, base_commit, problem statement, and any
        hints_text. Anything the vanilla baseline sees, the appmap variant
        must see too — otherwise the comparison is unfair."""
        instance_id = instance.get("instance_id", "")
        repo = instance.get("repo", "")
        base_commit = instance.get("base_commit", "")
        problem = (instance.get("problem_statement") or "").rstrip()
        hints = (instance.get("hints_text") or "").strip()

        parts = []
        if instance_id:
            parts.append(f"# Instance: {instance_id}")
        meta = []
        if repo:
            meta.append(f"Repository: {repo}")
        if base_commit:
            meta.append(f"Base commit: {base_commit}")
        if meta:
            parts.append("\n".join(meta))
        if problem:
            parts.append("## Problem description\n\n" + problem)
        if hints:
            parts.append("## Hints\n\n" + hints)
        return "\n\n".join(parts) + "\n"

    @staticmethod
    def _claude_md() -> str:
        """Project-level CLAUDE.md auto-loaded by claude on entry. Carries
        more contextual weight than the slash-command prompt. Re-states the
        appmap-fix workflow's hard constraints so they aren't drowned out by
        the agent's instinct to read source code."""
        return textwrap.dedent("""\
            # claude-appmap workflow — read me first

            This workspace is set up for AppMap-driven debugging. Follow these
            non-negotiable rules.

            ## Before reading any source file

            1. Run `bin/record-appmap.sh <test-command>` to produce at least one
               `.appmap.json` under `tmp/appmap/`.
            2. Inspect the recording via the **AppMap MCP tools** (prefix
               `mcp__appmap__`): `find_recordings`, `get_call_tree`,
               `find_calls`, `function_hotspots`, `sql_hotspots`,
               `list_labels`, etc.
            3. Only after MCP analysis indicates which functions actually run
               on the failing path may you `Read` or `Grep` files under the
               project's source tree.

            **Do NOT** open a project source file (`Read`, `Grep`, `Glob`)
            before steps 1 and 2 above. The whole point of running with
            AppMap is to let runtime data — not your priors — point you at
            the code that matters.

            ## If no existing test triggers the bug

            **Synthesize one**. Write a minimal test or script that exercises
            the failure path described in `issue.md`, place it under `tests/`
            (or wherever the project keeps tests), and pass it as the argument
            to `bin/record-appmap.sh`. Do not skip the recording step because
            "no existing test reproduces the bug" — that's exactly the case
            where you need to construct one.

            ## How to record (Python projects)

            `bin/record-appmap.sh` runs your test command inside the
            project's SWE-bench container with `appmap-python` instrumentation.
            Examples:

            ```bash
            # Django: project's own runner
            bin/record-appmap.sh ./tests/runtests.py --settings=test_sqlite \\
              migrations.test_repro_<issue_id>

            # pytest projects
            bin/record-appmap.sh pytest tests/test_repro_<issue_id>.py
            ```

            Recordings appear under `tmp/appmap/`; the host-side index watcher
            picks them up within seconds and the AppMap MCP can query them.

            ## Loop until fixed

            Do not end your turn until the agent has:
            (a) at least one AppMap recording, (b) at least one MCP query
            against it, and (c) an edit to a project source file that
            implements the fix. The `appmap.yml`, `.mcp.json`, `issue.md`,
            `bin/record-appmap.sh`, and this `CLAUDE.md` are scaffolding —
            edits to those don't count as a fix.
            """)

    @staticmethod
    def _gitignore_appmap_artifacts(clone: Path) -> None:
        """Append claude-appmap's runtime artifacts to .gitignore so the
        agent's actual code edits are the only thing in `git diff HEAD`."""
        marker = "# claude-appmap"
        addition = (
            f"\n{marker} runtime artifacts\n"
            "/tmp/appmap/\n"
            "/tmp/appmap-watch.log\n"
            "/appmap.log\n"
        )
        gi = clone / ".gitignore"
        existing = gi.read_text() if gi.exists() else ""
        if marker not in existing:
            gi.write_text(existing + addition)

    @staticmethod
    def _commit_scaffolding(clone: Path) -> None:
        """Commit appmap.yml/.mcp.json/issue.md/bin/record-appmap.sh/.gitignore
        as a new HEAD. patch_extractor.py uses `git diff HEAD`, so anything
        already in HEAD won't appear in the predicted patch."""
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "claude-appmap",
               "GIT_AUTHOR_EMAIL": "appmap@example.invalid",
               "GIT_COMMITTER_NAME": "claude-appmap",
               "GIT_COMMITTER_EMAIL": "appmap@example.invalid"}
        subprocess.run(
            ["git", "add", "-A", "appmap.yml", ".mcp.json", "issue.md",
             "CLAUDE.md", "bin/record-appmap.sh", "bin/run-tests.sh",
             ".gitignore"],
            cwd=str(clone), env=env, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--no-verify", "-m", "claude-appmap: scaffolding"],
            cwd=str(clone), env=env, capture_output=True,
        )

    @staticmethod
    def _appmap_yml(instance: Dict) -> str:
        """Minimal default. NO `packages:` section — the agent decides whether
        to add one.

        With no packages, AppMap-Python still records HTTP requests, SQL
        queries, exceptions, and any function bearing a built-in canonical
        label (`log`, `secret`, `security.*`, etc). That's usually enough to
        orient. Adding broad `packages:` (e.g. `path: .`) up-front floods the
        recording with thousands of intra-framework calls and forces every
        MCP query to filter through them. The appmap-fix skill instructs the
        agent to add packages one at a time, only when the built-ins prove
        insufficient."""
        name = instance.get("repo", "project").replace("/", "__")
        return textwrap.dedent(
            f"""\
            name: {name}
            language: python
            appmap_dir: tmp/appmap
            # packages: intentionally empty. Built-in instrumentation captures
            # HTTP requests, SQL, exceptions, and labeled functions. Add a
            # package here only when those built-ins haven't surfaced enough
            # detail — and add ONE at a time (mark adjacent deps `shallow: true`).
            """
        )

    @staticmethod
    def _mcp_json() -> str:
        # Stdio MCP. claude inherits cwd, so the MCP DB SHA matches the watcher's.
        return json.dumps(
            {
                "mcpServers": {
                    "appmap": {
                        "command": "node",
                        "args": [APPMAP_CLI_JS, "query", "mcp"],
                    }
                }
            },
            indent=2,
        )

    def _record_script(self, instance: Dict) -> str:
        """Wrapper script. Agent calls e.g.:
            ./bin/record-appmap.sh ./tests/runtests.py forms_tests
        which inside the container becomes:
            appmap-python ./tests/runtests.py forms_tests
        """
        image = self._test_spec.instance_image_key
        inner = textwrap.dedent(
            """\
            set -e
            source /opt/miniconda3/bin/activate testbed
            # Re-install editable so the agent's source edits take effect.
            python -m pip install -q -e . >/dev/null 2>&1 || true
            python -m pip install -q appmap >/dev/null 2>&1
            cd /testbed
            exec appmap-python "$@"
            """
        )
        return textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ $# -eq 0 ]]; then
              echo "usage: $0 <test-command...>" >&2
              echo "  e.g. $0 ./tests/runtests.py --settings=test_sqlite forms_tests" >&2
              exit 2
            fi
            CLONE="$(cd "$(dirname "$0")/.." && pwd)"
            exec podman run --rm \\
                -v "$CLONE":/testbed \\
                -w /testbed \\
                {shlex.quote(image)} \\
                bash -lc {shlex.quote(inner)} _wrap "$@"
            """
        )

    # ---- host-side AppMap process management ----------------------------

    @staticmethod
    def _seed_index(clone: Path) -> Optional[str]:
        """Create the query.db so `query mcp` can open it before any recordings exist.
        Returns the SHA prefix that locates the DB under ~/.appmap/data/<sha>/."""
        r = subprocess.run(
            ["node", APPMAP_CLI_JS, "index"],
            cwd=str(clone), check=False, capture_output=True, text=True,
        )
        m = claude_session.INDEX_DB_RE.search((r.stdout or "") + (r.stderr or ""))
        return m.group(1) if m else None

    @staticmethod
    def _start_watcher(clone: Path) -> subprocess.Popen:
        log = (clone / "tmp" / "appmap-watch.log").open("w")
        return subprocess.Popen(
            ["node", APPMAP_CLI_JS, "index", "--watch", "-d", str(clone)],
            cwd=str(clone), stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    @staticmethod
    def _stop_watcher(p: subprocess.Popen) -> None:
        if p.poll() is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=5)
        except Exception:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                pass

    # ---- live session log moved to utils.claude_session ----------------

    # ---- archive + cleanup -----------------------------------------------

    def _archive_appmap_data(self, clone: Path, instance_id: str, run_dir: Path) -> None:
        """Zip the clone's tmp/appmap/ into <run_dir>/appmap.zip."""
        appmap_dir = clone / "tmp" / "appmap"
        if not appmap_dir.exists() or not any(appmap_dir.iterdir()):
            print(f"  no appmap data to archive for {instance_id}", flush=True)
            return
        archive_path = run_dir / "appmap.zip"
        base = str(archive_path)[:-4]  # make_archive re-adds .zip
        shutil.make_archive(base, "zip", root_dir=str(appmap_dir))
        print(f"  archived appmap data → {archive_path}", flush=True)


    def _wipe_appmap_data(self, clone: Path) -> None:
        """Remove this run's appmap data and host index DB so the next run
        starts clean. Each run has a unique cwd so the SHA never collides,
        but cumulative cruft would still fill ~/.appmap/data over time."""
        appmap_dir = clone / "tmp" / "appmap"
        if appmap_dir.exists():
            shutil.rmtree(appmap_dir, ignore_errors=True)
        if self._index_sha:
            db_dir = Path.home() / ".appmap" / "data" / self._index_sha
            if db_dir.exists():
                shutil.rmtree(db_dir, ignore_errors=True)

    # ---- compatibility with PatchExtractor pipeline ---------------------

    def extract_file_changes(self, response: str) -> list:
        return []


def _fail(msg: str) -> Dict[str, object]:
    return {"success": False, "stdout": "", "stderr": msg, "returncode": -1}

"""
Claude Code backend with AppMap recording, **CLI flavor**.

Same as ClaudeAppMapMcpInterface except:
  - No `.mcp.json`. The agent does NOT get MCP tools advertised on every turn.
  - The agent uses the host `appmap query <verb>` CLI for analysis instead.
  - Saves ~3,000 tokens × N turns of cache_read on tool descriptions.

The watcher + index DB live on host as before — the CLI verbs read the
same query.db the MCP would. The agent invokes the CLI through Bash.
"""

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

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_TEMPLATE_PATH = REPO_ROOT / "prompts" / "synth_appmap_cli_solver.txt"


class ClaudeAppMapCliInterface:
    """Claude on host, recording sandbox in podman, AppMap via CLI (no MCP)."""

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

    # ---- public entrypoint ----------------------------------------------

    def execute_code_cli(
        self,
        prompt: str,
        cwd: str,
        model: Optional[str] = None,
        instance: Optional[Dict] = None,
    ) -> Dict[str, object]:
        if instance is None:
            return _fail("claude-appmap-cli requires the SWE-bench instance dict")

        clone = Path(cwd).resolve()
        instance_id = instance.get("instance_id", "unknown")
        if not str(clone).startswith(str(Path.home())):
            return _fail(
                f"clone {clone} is outside $HOME — podman cannot bind-mount it."
            )

        self._index_sha: Optional[str] = None
        try:
            self._ensure_instance_image(instance)
            self._write_workspace_files(clone, instance)
            self._index_sha = self._seed_index(clone)
        except Exception as e:
            return _fail(f"setup failed: {e}")

        run_dir = claude_session.resolve_run_dir(clone, instance_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        # Make `appmap` CLI available inside the agent's shell (it's a node
        # script; symlink wrapper in clone/bin so PATH discovery works).
        self._install_appmap_cli_wrapper(clone)

        session_id = str(uuid.uuid4())
        live_stop = claude_session.start_live_log(session_id, run_dir)

        watcher = self._start_watcher(clone)
        try:
            # NOTE: no --mcp-config / --strict-mcp-config. That's the whole
            # point of this variant — no MCP tool surface advertised.
            cmd = ["claude",
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

    # ---- per-instance setup ---------------------------------------------

    def _ensure_instance_image(self, instance: Dict) -> None:
        """Pull/build the SWE-bench prebuilt image; for synth fixtures the
        image is supplied by the run_synth driver (which monkey-patches this
        method)."""
        from swebench.harness.test_spec.test_spec import make_test_spec
        import docker, json as _json
        if not os.environ.get("DOCKER_HOST"):
            r = subprocess.run(["podman", "machine", "inspect"],
                               capture_output=True, text=True, check=False)
            if r.returncode == 0:
                for m in _json.loads(r.stdout):
                    p = m.get("ConnectionInfo", {}).get("PodmanSocket", {}).get("Path")
                    if p:
                        os.environ["DOCKER_HOST"] = f"unix://{p}"
                        break
        client = docker.from_env()
        spec = make_test_spec(instance, namespace="swebench")
        self._test_spec = spec
        image_ref = spec.instance_image_key
        for ref in (image_ref, f"docker.io/{image_ref}"):
            try:
                client.images.get(ref)
                return
            except docker.errors.ImageNotFound:
                continue
        repo, _, tag = image_ref.partition(":")
        client.images.pull(repo, tag=tag or "latest", platform="linux/amd64")

    def _write_workspace_files(self, clone: Path, instance: Dict) -> None:
        (clone / "appmap.yml").write_text(self._appmap_yml(instance))
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
        self._commit_scaffolding(clone)

    @staticmethod
    def _issue_md(instance: Dict) -> str:
        instance_id = instance.get("instance_id", "")
        repo = instance.get("repo", "")
        base_commit = instance.get("base_commit", "")
        problem = (instance.get("problem_statement") or "").rstrip()
        hints = (instance.get("hints_text") or "").strip()
        parts = []
        if instance_id:
            parts.append(f"# Instance: {instance_id}")
        meta = []
        if repo: meta.append(f"Repository: {repo}")
        if base_commit: meta.append(f"Base commit: {base_commit}")
        if meta: parts.append("\n".join(meta))
        if problem: parts.append("## Problem description\n\n" + problem)
        if hints: parts.append("## Hints\n\n" + hints)
        return "\n\n".join(parts) + "\n"

    @staticmethod
    def _claude_md() -> str:
        return textwrap.dedent("""\
            # claude-appmap-cli workspace — read me first

            This workspace is set up for AppMap-driven debugging via the
            host `appmap query` CLI (no MCP). The full diagnostic surface
            is one Bash call away:

                appmap query --help

            Verb-level help on demand:

                appmap query find --help
                appmap query tree --help

            The agent is expected to compose `appmap query <verb> | jq …`
            pipelines that pre-filter at the source rather than dump
            full results into context.

            See the appmap-fix skill (loaded at session start) for the
            full diagnostic loop.
            """)

    @staticmethod
    def _appmap_yml(instance: Dict) -> str:
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
    def _gitignore_appmap_artifacts(clone: Path) -> None:
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
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "claude-appmap-mcp",
               "GIT_AUTHOR_EMAIL": "appmap@example.invalid",
               "GIT_COMMITTER_NAME": "claude-appmap-mcp",
               "GIT_COMMITTER_EMAIL": "appmap@example.invalid"}
        subprocess.run(
            ["git", "add", "-A", "appmap.yml", "issue.md", "CLAUDE.md",
             "bin/record-appmap.sh", "bin/run-tests.sh", ".gitignore"],
            cwd=str(clone), env=env, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--no-verify", "-m", "claude-appmap-cli: scaffolding"],
            cwd=str(clone), env=env, capture_output=True,
        )

    def _record_script(self, instance: Dict) -> str:
        """Generic record-appmap.sh; the run_synth driver overrides this for
        non-SWE-bench fixtures via _record_script monkey-patch."""
        image = self._test_spec.instance_image_key
        inner = textwrap.dedent("""\
            set -e
            source /opt/miniconda3/bin/activate testbed
            python -m pip install -q -e . >/dev/null 2>&1 || true
            python -m pip install -q appmap >/dev/null 2>&1
            cd /testbed
            exec appmap-python "$@"
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
    def _install_appmap_cli_wrapper(clone: Path) -> None:
        """Create clone/bin/appmap that invokes the host node CLI. Means the
        agent can run plain `appmap query …` from its Bash without knowing
        the cli.js path."""
        bin_dir = clone / "bin"
        bin_dir.mkdir(exist_ok=True)
        wrapper = bin_dir / "appmap"
        wrapper.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            exec node {shlex.quote(APPMAP_CLI_JS)} "$@"
            """))
        wrapper.chmod(0o755)

    # ---- host-side AppMap process management ----------------------------

    @staticmethod
    def _seed_index(clone: Path) -> Optional[str]:
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
            try: os.killpg(p.pid, signal.SIGKILL)
            except Exception: pass

    # ---- archive + cleanup -----------------------------------------------

    def _archive_appmap_data(self, clone: Path, instance_id: str, run_dir: Path) -> None:
        appmap_dir = clone / "tmp" / "appmap"
        if not appmap_dir.exists() or not any(appmap_dir.iterdir()):
            print(f"  no appmap data to archive for {instance_id}", flush=True)
            return
        archive_path = run_dir / "appmap.zip"
        base = str(archive_path)[:-4]
        shutil.make_archive(base, "zip", root_dir=str(appmap_dir))
        print(f"  archived appmap data → {archive_path}", flush=True)

    def _wipe_appmap_data(self, clone: Path) -> None:
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

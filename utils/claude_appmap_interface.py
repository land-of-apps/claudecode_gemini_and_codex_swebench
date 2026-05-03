"""
SKETCH — Claude Code backend with AppMap recording.

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
import re
import shlex
import shutil
import signal
import subprocess
import textwrap
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


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
ARCHIVES_DIR = Path(
    os.environ.get("APPMAP_ARCHIVES_DIR", str(REPO_ROOT / "appmap_archives"))
)

_INDEX_DB_RE = re.compile(r"/\.appmap/data/([0-9a-f]+)/query\.db")

# Anthropic public list pricing as of 2026-05, USD per 1M tokens.
# Update or override with APPMAP_PRICING_JSON env var (path to JSON file with
# the same shape) when prices change. Cost estimates are for comparison
# between runs, not for billing.
_PRICING_PER_MTOK = {
    "claude-opus-4":     {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4":   {"input":  3.0, "output": 15.0, "cache_read": 0.30, "cache_write":  3.75},
    "claude-haiku-4":    {"input":  1.0, "output":  5.0, "cache_read": 0.10, "cache_write":  1.25},
}


class ClaudeAppMapInterface:
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

    # ---- public entrypoint ----------------------------------------------

    def execute_code_cli(
        self,
        prompt: str,                         # ignored: this backend builds its own
        cwd: str,                            # host clone dir
        model: Optional[str] = None,
        instance: Optional[Dict] = None,     # required; see wiring TODO
    ) -> Dict[str, object]:
        if instance is None:
            return _fail("claude-appmap requires the SWE-bench instance dict")

        clone = Path(cwd).resolve()
        instance_id = instance.get("instance_id", "unknown")
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
            self._ensure_instance_image(instance)         # pre-build sweb.eval.* image
            self._write_workspace_files(clone, instance)
            self._index_sha = self._seed_index(clone)     # create empty query.db, capture sha
        except Exception as e:
            return _fail(f"setup failed: {e}")

        # Hoist run_dir up-front so the live-log thread can write into it.
        run_dir = self._resolve_run_dir(clone, instance_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        # Pinning the session id makes the source JSONL deterministically
        # findable; the live-log thread then symlinks it into run_dir so
        # session.jsonl reflects the agent's writes in real time, and
        # console.log carries a human-readable per-step stream.
        session_id = str(uuid.uuid4())
        live_stop = self._start_live_log(session_id, run_dir)

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
            # Archive recordings + claude session logs alongside the clone so
            # the run is self-contained for analysis. Then wipe the host index
            # DB for this run's SHA so ~/.appmap/data doesn't grow over 300
            # instances.
            try:
                self._archive_appmap_data(clone, instance_id)
            except Exception as e:
                print(f"  warning: failed to archive appmap data: {e}", flush=True)
            try:
                self._archive_session_logs(clone, instance_id)
            except Exception as e:
                print(f"  warning: failed to archive session logs: {e}", flush=True)
            try:
                self._compute_and_save_usage(clone, instance_id)
            except Exception as e:
                print(f"  warning: failed to compute usage stats: {e}", flush=True)
            try:
                self._wipe_appmap_data(clone)
            except Exception as e:
                print(f"  warning: failed to wipe appmap data: {e}", flush=True)

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
        (clone / "appmap.yml").write_text(self._appmap_yml(instance))
        (clone / ".mcp.json").write_text(self._mcp_json())
        (clone / "issue.md").write_text(instance.get("problem_statement", ""))
        (clone / "CLAUDE.md").write_text(self._claude_md())
        (clone / "tmp" / "appmap").mkdir(parents=True, exist_ok=True)
        bin_dir = clone / "bin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "record-appmap.sh"
        script.write_text(self._record_script(instance))
        script.chmod(0o755)
        self._gitignore_appmap_artifacts(clone)
        self._commit_scaffolding(clone)

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
             "CLAUDE.md", "bin/record-appmap.sh", ".gitignore"],
            cwd=str(clone), env=env, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--no-verify", "-m", "claude-appmap: scaffolding"],
            cwd=str(clone), env=env, capture_output=True,
        )

    @staticmethod
    def _appmap_yml(instance: Dict) -> str:
        name = instance.get("repo", "project").replace("/", "__")
        return textwrap.dedent(
            f"""\
            name: {name}
            language: python
            appmap_dir: tmp/appmap
            packages:
            - path: .
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
        m = _INDEX_DB_RE.search((r.stdout or "") + (r.stderr or ""))
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

    # ---- live session log -----------------------------------------------

    def _start_live_log(self, session_id: str, run_dir: Path) -> threading.Event:
        """Symlink the claude session JSONL into run_dir as soon as it appears,
        and stream a human-readable console.log of tool calls + assistant text
        in parallel. Returns a stop Event the caller sets when claude exits."""
        stop = threading.Event()

        def watcher() -> None:
            projects = Path.home() / ".claude" / "projects"
            symlink = run_dir / "session.jsonl"
            console = run_dir / "console.log"
            src: Optional[Path] = None
            # Wait for claude to create the file. claude only writes once it
            # processes the first prompt event, so a few seconds is normal.
            deadline = time.time() + 60
            while not stop.is_set() and src is None and time.time() < deadline:
                matches = list(projects.glob(f"*/{session_id}.jsonl"))
                if matches:
                    src = matches[0]
                    break
                time.sleep(0.5)
            if src is None:
                return
            try:
                if symlink.exists() or symlink.is_symlink():
                    symlink.unlink()
                symlink.symlink_to(src)
            except Exception as e:
                print(f"  warning: session symlink failed: {e}", flush=True)
            try:
                with src.open("r", errors="ignore") as f, console.open("a") as out:
                    out.write(f"# session_id={session_id}\n# source={src}\n")
                    out.flush()
                    buf = ""
                    while not stop.is_set():
                        chunk = f.read()
                        if chunk:
                            buf += chunk
                            while "\n" in buf:
                                line, buf = buf.split("\n", 1)
                                formatted = self._format_session_line(line)
                                if formatted:
                                    out.write(formatted + "\n")
                                    out.flush()
                        else:
                            time.sleep(0.5)
            except Exception as e:
                print(f"  warning: live log writer stopped: {e}", flush=True)

        threading.Thread(target=watcher, daemon=True).start()
        return stop

    @staticmethod
    def _format_session_line(line: str) -> Optional[str]:
        """Turn one JSONL record into 0+ human-readable lines.

        No content is truncated — session.jsonl already has the raw form, but
        this view should also preserve everything for readability without
        forcing the user to re-parse JSON. Multi-line tool inputs / results
        are wrapped with a continuation indent so the "one event per stanza"
        rhythm survives."""
        try:
            d = json.loads(line)
        except Exception:
            return None
        ts = (d.get("timestamp") or "")[11:19]
        msg = d.get("message", {})
        if not isinstance(msg, dict):
            return None
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
            if not text:
                return None
            return ClaudeAppMapInterface._wrap_lines(
                f"[{ts}] {msg.get('role','?').upper()}: ", text
            )
        if not isinstance(content, list):
            return None
        out: List[str] = []
        for c in content:
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "tool_use":
                name = c.get("name", "?")
                summary = ClaudeAppMapInterface._summarize_tool_input(name, c.get("input", {}))
                out.append(ClaudeAppMapInterface._wrap_lines(f"[{ts}] → {name}(", summary, suffix=")"))
            elif t == "text":
                text = (c.get("text") or "").strip()
                if text:
                    out.append(ClaudeAppMapInterface._wrap_lines(f"[{ts}]   ", text))
            elif t == "tool_result":
                o = c.get("content", "")
                if isinstance(o, list):
                    o = (o[0] or {}).get("text", "") if o else ""
                o = str(o).strip()
                if o:
                    out.append(ClaudeAppMapInterface._wrap_lines(f"[{ts}]   ← ", o))
        return "\n".join(out) if out else None

    @staticmethod
    def _wrap_lines(prefix: str, body: str, suffix: str = "") -> str:
        """Emit body under prefix, prepending a continuation indent on
        subsequent lines so the timestamp column stays aligned."""
        lines = body.splitlines() or [""]
        indent = " " * len(prefix)
        first = prefix + lines[0]
        if len(lines) == 1:
            return first + suffix
        rest = [indent + line for line in lines[1:]]
        if suffix:
            rest[-1] = rest[-1] + suffix
        return "\n".join([first, *rest])

    @staticmethod
    def _summarize_tool_input(name: str, inp: Dict) -> str:
        """Pick the most informative field for a tool call. Returns full
        content (no truncation) — the wrapper handles multi-line layout."""
        if not isinstance(inp, dict):
            return ""
        if name in ("Read", "Edit", "Write"):
            base = inp.get("file_path", "")
            extras = []
            for k in ("offset", "limit", "old_string", "new_string", "content"):
                v = inp.get(k)
                if v is not None:
                    extras.append(f"{k}={v!r}" if isinstance(v, (int, str)) and len(str(v)) < 60
                                  else f"{k}=<{len(str(v))} chars>")
            return base + (" " + ", ".join(extras) if extras else "")
        if name == "Bash":
            cmd = inp.get("command", "")
            desc = inp.get("description", "")
            return f"{cmd}" + (f"  # {desc}" if desc else "")
        if name == "Grep":
            return f"{inp.get('pattern','')}" + (
                f" in {inp.get('path','')}" if inp.get("path") else "")
        if name == "Glob":
            return inp.get("pattern", "")
        if name.startswith("mcp__"):
            return ", ".join(f"{k}={json.dumps(v) if not isinstance(v, str) else v!r}"
                             for k, v in inp.items())
        # Fallback: show whole input as JSON so nothing is hidden
        return json.dumps(inp, ensure_ascii=False)

    # ---- archive + cleanup -----------------------------------------------

    def _resolve_run_dir(self, clone: Path, instance_id: str) -> Path:
        """Return the per-run directory that holds outputs for this run.

        When the orchestrator has placed the clone at ./work/<id>/<ts>/repo,
        return ./work/<id>/<ts>. Otherwise fall back to a fresh
        <repo_root>/appmap_archives/<id>_<ts>/ so smoke runs (whose clones
        live elsewhere) still get a stable archive location."""
        work_dir = clone.parent  # ./work/<id>/<ts>/
        if work_dir.name and work_dir.parent.name == instance_id and \
           work_dir.parent.parent.name == "work":
            return work_dir
        ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = ARCHIVES_DIR / f"{instance_id}_{ts}"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    def _archive_appmap_data(self, clone: Path, instance_id: str) -> None:
        """Zip the clone's tmp/appmap/ into <run_dir>/appmap.zip."""
        appmap_dir = clone / "tmp" / "appmap"
        if not appmap_dir.exists() or not any(appmap_dir.iterdir()):
            print(f"  no appmap data to archive for {instance_id}", flush=True)
            return
        run_dir = self._resolve_run_dir(clone, instance_id)
        archive_path = run_dir / "appmap.zip"
        base = str(archive_path)[:-4]  # make_archive re-adds .zip
        shutil.make_archive(base, "zip", root_dir=str(appmap_dir))
        print(f"  archived appmap data → {archive_path}", flush=True)

    def _archive_session_logs(self, clone: Path, instance_id: str) -> None:
        """Copy claude session JSONLs whose `cwd` matches this clone into
        <run_dir>/sessions/. Claude appends to these files in real time
        as it works, so they're the canonical work log."""
        projects = Path.home() / ".claude" / "projects"
        if not projects.is_dir():
            return
        needle = f'"cwd":"{clone}"'
        target_root = self._resolve_run_dir(clone, instance_id) / "sessions"
        copied: List[str] = []
        for jsonl in projects.glob("*/*.jsonl"):
            try:
                # cwd is recorded near the top; cap the read to bound cost.
                head = jsonl.read_text(errors="ignore")[:32768]
            except Exception:
                continue
            if needle in head:
                target_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(jsonl, target_root / jsonl.name)
                copied.append(jsonl.name)
        if copied:
            print(f"  archived {len(copied)} session log(s) → {target_root}", flush=True)

    def _compute_and_save_usage(self, clone: Path, instance_id: str) -> None:
        """Aggregate per-message `usage` blocks from the archived session logs
        and write <run_dir>/usage.json with token totals + estimated $ cost.

        For comparing claude vs claude-appmap on the same instance the raw
        token counts are the durable signal; cost is a snapshot of public
        list pricing (see _PRICING_PER_MTOK)."""
        run_dir = self._resolve_run_dir(clone, instance_id)
        sessions_dir = run_dir / "sessions"
        if not sessions_dir.is_dir():
            return

        pricing = self._load_pricing()
        per_model: Dict[str, Dict[str, int]] = {}
        first_ts: Optional[str] = None
        last_ts: Optional[str] = None

        for jsonl in sorted(sessions_dir.glob("*.jsonl")):
            with jsonl.open(errors="ignore") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ts = d.get("timestamp")
                    if isinstance(ts, str):
                        if first_ts is None or ts < first_ts:
                            first_ts = ts
                        if last_ts is None or ts > last_ts:
                            last_ts = ts
                    msg = d.get("message", {}) or {}
                    if not isinstance(msg, dict):
                        continue
                    model = msg.get("model")
                    usage = msg.get("usage")
                    if not (model and isinstance(usage, dict)):
                        continue
                    bucket = per_model.setdefault(model, {
                        "messages": 0,
                        "input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 0,
                    })
                    bucket["messages"] += 1
                    for k in ("input_tokens", "cache_read_input_tokens",
                              "cache_creation_input_tokens", "output_tokens"):
                        v = usage.get(k, 0)
                        if isinstance(v, int):
                            bucket[k] += v

        models_out: Dict[str, Dict[str, object]] = {}
        totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 0,
                  "estimated_cost_usd": 0.0}
        for model, b in per_model.items():
            cost = self._estimate_cost(model, b, pricing)
            models_out[model] = {**b, "estimated_cost_usd": round(cost, 6)}
            for k in ("input_tokens", "cache_read_input_tokens",
                      "cache_creation_input_tokens", "output_tokens"):
                totals[k] += b[k]
            totals["estimated_cost_usd"] += cost
        totals["estimated_cost_usd"] = round(totals["estimated_cost_usd"], 6)

        wall_seconds: Optional[float] = None
        if first_ts and last_ts:
            try:
                wall_seconds = (datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                                - datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                                ).total_seconds()
            except Exception:
                pass

        out = {
            "instance_id": instance_id,
            "first_timestamp": first_ts,
            "last_timestamp": last_ts,
            "wall_seconds": wall_seconds,
            "models": models_out,
            "totals": totals,
            "pricing_source": ("env:APPMAP_PRICING_JSON" if os.environ.get("APPMAP_PRICING_JSON")
                               else "builtin: see _PRICING_PER_MTOK"),
        }
        (run_dir / "usage.json").write_text(json.dumps(out, indent=2))
        print(
            f"  usage: {totals['input_tokens']:,} in, "
            f"{totals['output_tokens']:,} out, "
            f"{totals['cache_read_input_tokens']:,} cache-read; "
            f"~${totals['estimated_cost_usd']:.4f}",
            flush=True,
        )

    @staticmethod
    def _load_pricing() -> Dict[str, Dict[str, float]]:
        override = os.environ.get("APPMAP_PRICING_JSON")
        if override and Path(override).is_file():
            try:
                return json.loads(Path(override).read_text())
            except Exception:
                pass
        return _PRICING_PER_MTOK

    @staticmethod
    def _estimate_cost(model: str, usage: Dict[str, int],
                       pricing: Dict[str, Dict[str, float]]) -> float:
        # Match by family prefix so e.g. claude-opus-4-7 falls under claude-opus-4.
        rates = None
        for family, r in pricing.items():
            if model.startswith(family):
                rates = r
                break
        if not rates:
            return 0.0
        per_tok = lambda key: rates.get(key, 0.0) / 1_000_000.0
        cost = (
            usage.get("input_tokens", 0)               * per_tok("input")
            + usage.get("output_tokens", 0)            * per_tok("output")
            + usage.get("cache_read_input_tokens", 0)  * per_tok("cache_read")
            + usage.get("cache_creation_input_tokens", 0) * per_tok("cache_write")
        )
        return cost

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

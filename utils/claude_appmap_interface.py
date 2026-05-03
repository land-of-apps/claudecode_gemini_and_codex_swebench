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

        watcher = self._start_watcher(clone)
        try:
            cmd = ["claude",
                   "--mcp-config", str(clone / ".mcp.json"),
                   "--strict-mcp-config",
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
        (clone / "tmp" / "appmap").mkdir(parents=True, exist_ok=True)
        bin_dir = clone / "bin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "record-appmap.sh"
        script.write_text(self._record_script(instance))
        script.chmod(0o755)

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

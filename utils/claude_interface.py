"""Vanilla `claude` backend.

Runs claude on the host with the SWE-bench prompt on stdin. Mirrors the
session log + console output + cost stats into the run dir using the
shared machinery in `utils.claude_session`, so the post-run artifacts
match what `claude-appmap` produces (the only difference is no AppMap
recordings)."""

import os
import subprocess
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

from utils import claude_session

load_dotenv()


class ClaudeCodeInterface:
    """Interface for interacting with Claude Code CLI."""

    def __init__(self):
        try:
            r = subprocess.run(["claude", "--version"], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(
                    "Claude CLI not found. Please ensure 'claude' is installed and in PATH"
                )
        except FileNotFoundError:
            raise RuntimeError(
                "Claude CLI not found. Please ensure 'claude' is installed and in PATH"
            )

    def execute_code_cli(
        self,
        prompt: str,
        cwd: str,
        model: Optional[str] = None,
        instance: Optional[Dict] = None,
    ) -> Dict[str, object]:
        """Execute Claude Code via CLI and capture the response.

        Args:
            prompt: The prompt to send to Claude (initial stdin input).
            cwd: Working directory (the cloned repo).
            model: Optional model alias / full ID.
            instance: SWE-bench instance dict; only used to derive the
                instance_id for the per-run output directory. Optional for
                back-compat with callers that don't pass it.
        """
        clone = Path(cwd).resolve()
        instance_id = (instance or {}).get("instance_id", clone.parent.parent.name
                                            if clone.parent.parent.name != "work" else "unknown")
        run_dir = claude_session.resolve_run_dir(clone, instance_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        session_id = str(uuid.uuid4())
        live_stop = claude_session.start_live_log(session_id, run_dir)

        cmd = ["claude",
               "--session-id", session_id,
               "--dangerously-skip-permissions"]
        if model:
            cmd.extend(["--model", model])

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                cwd=str(clone),
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("CLAUDE_TIMEOUT", "1800")),
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Command timed out after CLAUDE_TIMEOUT seconds",
                "returncode": -1,
            }
        except Exception as e:
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
            }
        finally:
            live_stop.set()
            try:
                claude_session.archive_session_logs(clone, run_dir)
            except Exception as e:
                print(f"  warning: failed to archive session logs: {e}", flush=True)
            try:
                claude_session.compute_and_save_usage(run_dir, instance_id)
            except Exception as e:
                print(f"  warning: failed to compute usage stats: {e}", flush=True)

    def extract_file_changes(self, response: str) -> List[Dict[str, str]]:
        return []

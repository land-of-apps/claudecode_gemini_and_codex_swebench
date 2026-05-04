"""Try the appmap-rca subagent against an existing claude-appmap-mcp run.

Reuses the source run's repo + recordings + scaffolding. Drops the
RCA agent definition into <new-repo>/.claude/agents/. Invokes claude
with a paper-thin main prompt that dispatches to the subagent and
prints back the report verbatim — main agent does NO investigation
or fix attempts of its own.

Usage:
  ./venv/bin/python tools/try_rca.py work/claude-appmap-mcp/synth__oscar_4016/20260504_111927
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utils import claude_session  # noqa: E402

APPMAP_CLI_JS = os.environ.get(
    "APPMAP_CLI_JS",
    "/Users/kgilpin/source/appland/appmap-js/packages/cli/built/cli.js",
)
RCA_AGENT_SRC = REPO_ROOT / "agents" / "appmap-rca.md"


def _discover_podman_socket():
    if os.environ.get("DOCKER_HOST"):
        return os.environ["DOCKER_HOST"]
    try:
        import json
        r = subprocess.run(["podman", "machine", "inspect"],
                           capture_output=True, text=True, check=True)
        for m in json.loads(r.stdout):
            p = m.get("ConnectionInfo", {}).get("PodmanSocket", {}).get("Path")
            if p:
                return f"unix://{p}"
    except Exception:
        return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source_run",
                    help="Path to a prior run dir (must contain repo/ and "
                         "ideally appmap.zip).")
    ap.add_argument("--model", default="claude-opus-4-7")
    ap.add_argument("--timeout", type=int, default=900,
                    help="claude wall-time cap (s). Default 900.")
    args = ap.parse_args()

    source = Path(args.source_run).resolve()
    src_repo = source / "repo"
    if not src_repo.is_dir():
        print(f"no repo/ under {source}", file=sys.stderr)
        sys.exit(2)
    if not (src_repo / ".mcp.json").is_file():
        print(f"source repo lacks .mcp.json (was this a claude-appmap-mcp run?)",
              file=sys.stderr)
        sys.exit(2)
    if not RCA_AGENT_SRC.is_file():
        print(f"missing {RCA_AGENT_SRC}", file=sys.stderr)
        sys.exit(2)

    fixture_id = source.parent.name
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = REPO_ROOT / "work" / "experiments" / "rca-only" / fixture_id / ts
    repo = run_dir / "repo"
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"copying {src_repo} → {repo}", flush=True)

    # Skip the source's tmp/ — stale appmap watchers may still be writing
    # to it, which races copytree, and we recreate it from appmap.zip below.
    def _skip_tmp(srcdir, names):
        if Path(srcdir).resolve() == src_repo:
            return [n for n in names if n == "tmp"]
        return []

    shutil.copytree(src_repo, repo, symlinks=True, ignore=_skip_tmp)

    # Restore the previous run's recordings into tmp/appmap/
    appmap_zip = source / "appmap.zip"
    appmap_dir = repo / "tmp" / "appmap"
    appmap_dir.mkdir(parents=True, exist_ok=True)
    if appmap_zip.is_file():
        with zipfile.ZipFile(appmap_zip) as z:
            z.extractall(appmap_dir)
        n = sum(1 for _ in appmap_dir.rglob("*.appmap.json"))
        print(f"  restored {n} recordings from {appmap_zip.name}", flush=True)
    else:
        print("  WARNING: no appmap.zip — RCA may need to re-record", flush=True)

    # Drop the RCA subagent definition into the project-scoped agents dir.
    agents_dir = repo / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(RCA_AGENT_SRC, agents_dir / "appmap-rca.md")
    print(f"  wrote {agents_dir.relative_to(repo)}/appmap-rca.md", flush=True)

    # Override CLAUDE.md so the main agent doesn't read the prior workflow's
    # instructions (which told *it* to use AppMap MCP directly).
    claude_md = textwrap.dedent("""\
        # rca-only experiment

        You are testing a single-step strategy: dispatch the
        appmap-rca subagent to identify the root cause of the bug
        in `issue.md`. **Do NOT investigate yourself.** Do not read
        source files, run commands, or call AppMap tools directly.

        Your only actions:
        1. Read `issue.md`.
        2. Dispatch via `Agent(subagent_type="appmap-rca", ...)`,
           passing the bug report and noting that recordings already
           exist under `tmp/appmap/`.
        3. Print the subagent's report verbatim.
        4. Stop. Do NOT attempt to fix the bug.
        """)
    (repo / "CLAUDE.md").write_text(claude_md)

    if not os.environ.get("DOCKER_HOST"):
        sock = _discover_podman_socket()
        if sock:
            os.environ["DOCKER_HOST"] = sock

    # Seed the AppMap index for this new cwd so the MCP DB exists.
    print("seeding AppMap index...", flush=True)
    seed = subprocess.run(
        ["node", APPMAP_CLI_JS, "index"],
        cwd=str(repo), capture_output=True, text=True, check=False,
    )
    sha_match = claude_session.INDEX_DB_RE.search((seed.stdout or "") + (seed.stderr or ""))
    sha = sha_match.group(1) if sha_match else None
    if sha:
        print(f"  index sha: {sha}", flush=True)
    else:
        print(f"  WARNING: could not parse index DB SHA from output", flush=True)
        print((seed.stdout or "") + (seed.stderr or ""), flush=True)

    # Start the watcher (in case the agent re-records).
    watch_log = (repo / "tmp" / "appmap-watch.log").open("w")
    watcher = subprocess.Popen(
        ["node", APPMAP_CLI_JS, "index", "--watch", "-d", str(repo)],
        cwd=str(repo), stdout=watch_log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    # Compose the main prompt. Paper-thin — the work is in the subagent.
    main_prompt = textwrap.dedent("""\
        Read `issue.md`, then dispatch the `appmap-rca` subagent to
        identify the root cause.

        Brief the subagent in the dispatch prompt:
        - Paste the contents of issue.md.
        - Mention that AppMap recordings of the failing scenario
          already exist under `tmp/appmap/` (left over from a prior
          investigation), so it should call `find_recordings` first
          before attempting to record anything new.
        - Ask for the standard RCA report format.

        When the subagent returns, print its report verbatim. Then
        stop. **Do not attempt to fix the bug.** This experiment is
        testing only Step 1 of the workflow.
        """)

    # Set up live session log mirroring (gives us session.jsonl, console.log,
    # and incremental usage.json under run_dir).
    session_id = str(uuid.uuid4())
    live_stop = claude_session.start_live_log(session_id, run_dir)

    cmd = [
        "claude",
        "--mcp-config", str(repo / ".mcp.json"),
        "--strict-mcp-config",
        "--session-id", session_id,
        "--dangerously-skip-permissions",
        "--model", args.model,
    ]

    print(f"\nrunning claude (session {session_id})...", flush=True)
    print(f"  cwd={repo}", flush=True)
    try:
        result = subprocess.run(
            cmd,
            input=main_prompt,
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
        rc = result.returncode
        out = result.stdout
        err = result.stderr
    except subprocess.TimeoutExpired:
        rc = -1
        out = ""
        err = f"timeout after {args.timeout}s"
    finally:
        # Stop watcher
        try:
            os.killpg(watcher.pid, signal.SIGTERM)
            watcher.wait(timeout=5)
        except Exception:
            try:
                os.killpg(watcher.pid, signal.SIGKILL)
            except Exception:
                pass
        live_stop.set()

    # Persist the agent's stdout transcript and stderr alongside the live log
    (run_dir / "stdout.txt").write_text(out)
    (run_dir / "stderr.txt").write_text(err)

    # Archive session logs + compute usage (same as the main backends)
    try:
        claude_session.archive_session_logs(repo, run_dir)
    except Exception as e:
        print(f"  warning: archive_session_logs: {e}", flush=True)
    try:
        claude_session.compute_and_save_usage(run_dir, f"rca-only-{fixture_id}")
    except Exception as e:
        print(f"  warning: compute_and_save_usage: {e}", flush=True)

    print(f"\nrc={rc}")
    if err:
        print("--- stderr (tail) ---")
        print(err[-1500:])
    print(f"\nrun_dir → {run_dir}")


if __name__ == "__main__":
    main()

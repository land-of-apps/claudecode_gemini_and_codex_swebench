"""Step 2 of the 3-step strategy: apply a fix from an RCA report.

Picks up an RCA experiment's run dir, extracts the subagent's report,
resets the working tree to HEAD (so the bug is present), and runs
claude with a fix-only prompt — no AppMap MCP, no subagent. The
main agent's job is narrow: read the cited lines, edit, run tests.

Usage:
  ./venv/bin/python tools/try_code_fix.py work/experiments/rca-only/synth__oscar_4016/20260504_125152
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utils import claude_session  # noqa: E402


def _extract_rca_report(session_jsonl: Path) -> str:
    """Pull the appmap-rca subagent's report out of the parent's
    session JSONL. The Agent tool's result has a content array; the
    final text item is the subagent's last message — that's the
    report."""
    if not session_jsonl.exists():
        # Symlink may point under ~/.claude/projects/. Resolve.
        session_jsonl = session_jsonl.resolve()
    with session_jsonl.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "user":
                continue
            for item in rec.get("message", {}).get("content", []) or []:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                # Last text block is the subagent's final message.
                texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                if not texts:
                    continue
                # Strip any trailing <usage>...</usage> metadata block
                report = texts[0]
                marker = "agentId:"
                if marker in report:
                    report = report.split(marker)[0].rstrip()
                # Heuristic: a real RCA report mentions "Root cause" or
                # the subagent's structured headings.
                if "## Root cause" in report or "## Evidence" in report:
                    return report
    raise RuntimeError(f"could not find RCA report in {session_jsonl}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source_rca_run",
                    help="Path to a try_rca.py output dir (must have repo/ "
                         "and session.jsonl with the rca subagent's report).")
    ap.add_argument("--model", default="claude-opus-4-7")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    source = Path(args.source_rca_run).resolve()
    src_repo = source / "repo"
    src_session = source / "session.jsonl"
    if not src_repo.is_dir():
        print(f"no repo/ under {source}", file=sys.stderr); sys.exit(2)
    if not src_session.exists():
        print(f"no session.jsonl under {source}", file=sys.stderr); sys.exit(2)

    print(f"extracting RCA report from {src_session.name}...", flush=True)
    rca_report = _extract_rca_report(src_session)
    print(f"  report: {len(rca_report):,} chars", flush=True)

    # Pull fixture id from the path: .../synth__<id>/<ts>/
    fixture_id = source.parent.name
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = REPO_ROOT / "work" / "experiments" / "code-fix" / fixture_id / ts
    repo = run_dir / "repo"
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"copying {src_repo} → {repo}", flush=True)

    # Skip the source's tmp/ to avoid races with stale watchers AND
    # because main-agent fix step doesn't need recordings. Also skip
    # .claude/ so the rca subagent definition isn't carried forward.
    def _skip(srcdir, names):
        if Path(srcdir).resolve() == src_repo:
            return [n for n in names if n in ("tmp", ".claude")]
        return []

    shutil.copytree(src_repo, repo, symlinks=True, ignore=_skip)

    # Reset working tree to HEAD so the bug is present (the source RCA
    # repo's working tree had a prior fix applied; we want to test the
    # main agent's ability to fix from scratch given an RCA report).
    print("resetting working tree to HEAD...", flush=True)
    subprocess.run(["git", "checkout", "HEAD", "--", "."],
                   cwd=str(repo), check=True)
    subprocess.run(["git", "clean", "-fd",
                    "--exclude=bin/", "--exclude=appmap.yml",
                    "--exclude=.mcp.json", "--exclude=issue.md",
                    "--exclude=CLAUDE.md", "--exclude=.gitignore"],
                   cwd=str(repo), check=False)
    # Re-anchor base_commit
    base_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), text=True).strip()

    # Strip the appmap MCP config — main agent doesn't need it for fix-only.
    mcp_path = repo / ".mcp.json"
    if mcp_path.exists():
        mcp_path.unlink()

    # Write a focused CLAUDE.md that tells main what its narrow job is.
    claude_md = textwrap.dedent("""\
        # code-fix-only experiment

        A root-cause analysis was already performed for the bug in
        `issue.md`. The RCA report is included in your initial user
        message.

        Your narrow job: **apply the fix that the RCA prescribes.**

        - Read the file:line locations the RCA cites.
        - Make the minimum edit that resolves the cause as described.
        - Run `bin/run-tests.sh pytest <relevant tests>` to confirm.
        - Run a slightly broader test pass for regressions in
          adjacent modules (basket, offer, voucher).
        - Stop.

        Do **not** re-investigate. Do **not** call AppMap tools (none
        are wired up here anyway). The diagnosis is settled — your
        contribution is the edit and the test confirmation.
        """)
    (repo / "CLAUDE.md").write_text(claude_md)

    # Compose the user message: bug context + RCA report + ask.
    issue_md = (repo / "issue.md").read_text() if (repo / "issue.md").exists() else ""
    user_prompt = textwrap.dedent(f"""\
        Apply the fix prescribed by the root-cause analysis below.

        ## Bug report (issue.md)

        {issue_md.strip()}

        ## Root-cause analysis (from appmap-rca subagent)

        {rca_report}

        ## What to do

        1. Read the file:line locations the RCA cites.
        2. Make the minimum edit that resolves the cause.
        3. Run `bin/run-tests.sh pytest tests/integration/basket/test_utils.py`
           and one or two adjacent test modules. The container has
           pytest pre-installed; do not pip install on the host.
        4. Stop. Do not re-investigate, do not call AppMap tools.
        """)

    # Run claude (no --mcp-config — fix step doesn't need appmap).
    session_id = str(uuid.uuid4())
    live_stop = claude_session.start_live_log(session_id, run_dir)

    cmd = [
        "claude",
        "--session-id", session_id,
        "--dangerously-skip-permissions",
        "--model", args.model,
    ]

    print(f"\nrunning claude (session {session_id})...", flush=True)
    print(f"  cwd={repo}", flush=True)
    try:
        result = subprocess.run(
            cmd,
            input=user_prompt,
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
        live_stop.set()

    (run_dir / "stdout.txt").write_text(out)
    (run_dir / "stderr.txt").write_text(err)
    (run_dir / "rca_report.md").write_text(rca_report)

    # Capture the diff produced by main agent.
    diff = subprocess.check_output(
        ["git", "diff", "HEAD"], cwd=str(repo), text=True)
    (run_dir / "diff.patch").write_text(diff)

    try:
        claude_session.archive_session_logs(repo, run_dir)
    except Exception as e:
        print(f"  warning: archive_session_logs: {e}", flush=True)
    try:
        claude_session.compute_and_save_usage(run_dir, f"code-fix-{fixture_id}")
    except Exception as e:
        print(f"  warning: compute_and_save_usage: {e}", flush=True)

    print(f"\nrc={rc}")
    if err:
        print("--- stderr (tail) ---")
        print(err[-1000:])
    print(f"diff: {len(diff):,} chars")
    print(f"run_dir → {run_dir}")


if __name__ == "__main__":
    main()

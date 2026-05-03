"""Backfill console.log + sessions/ + usage.json for any run dir under
./work/ that's missing them.

Useful when a run was launched before the shared session-mirror code
existed (or when artifacts were deleted). Finds matching session JSONLs
in ~/.claude/projects/ by `cwd` substring match against the run's repo
path, then formats them through utils.claude_session.format_session_line.

Usage:
  ./venv/bin/python tools/regen_console.py [--force]

  --force   Rewrite console.log even if it already exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utils.claude_session import (
    format_session_line, archive_session_logs, compute_and_save_usage,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="Rewrite console.log even if it already exists")
    args = ap.parse_args()

    work = REPO_ROOT / "work"
    projects = Path.home() / ".claude" / "projects"
    if not work.is_dir():
        print(f"no work dir: {work}", file=sys.stderr)
        sys.exit(1)

    # Walk: work/<backend>/<id>/<ts>/repo
    for repo in sorted(work.glob("*/django__django-*/*/repo")):
        run_dir = repo.parent
        clone = repo
        instance_id = run_dir.parent.name
        backend = run_dir.parent.parent.name
        console = run_dir / "console.log"
        if console.exists() and not args.force:
            continue

        needle = f'"cwd":"{clone}"'
        sessions = []
        for jsonl in projects.glob("*/*.jsonl"):
            try:
                head = jsonl.read_text(errors="ignore")[:32768]
            except Exception:
                continue
            if needle in head:
                sessions.append(jsonl)
        if not sessions:
            print(f"  no session matches for {backend}/{instance_id}/{run_dir.name}")
            continue
        sessions.sort(key=lambda p: p.stat().st_mtime)

        with console.open("w") as out:
            for s in sessions:
                out.write(f"# session_id={s.stem}\n# source={s}\n")
                with s.open(errors="ignore") as f:
                    for line in f:
                        formatted = format_session_line(line)
                        if formatted:
                            out.write(formatted + "\n")
                out.write("\n")

        # Mirror the post-run archive + usage steps so the dir matches
        # what a fresh run would produce.
        archive_session_logs(clone, run_dir)
        compute_and_save_usage(run_dir, instance_id)
        print(f"regen {backend:14s} {instance_id:30s} {run_dir.name}  "
              f"({len(sessions)} session(s), {console.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()

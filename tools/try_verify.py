"""Step 3 of the 3-step strategy: dispatch appmap-verify against a patched repo.

Picks up the output of try_code_fix.py — a repo with the fix applied —
and runs claude with a paper-thin main prompt that dispatches the
appmap-verify subagent. The subagent re-records under the fixed code,
compares the runtime to the expected change, returns PASS/FAIL.

Usage:
  ./venv/bin/python tools/try_verify.py work/experiments/code-fix/synth__oscar_4016/<ts>
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
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utils import claude_session  # noqa: E402

APPMAP_CLI_JS = os.environ.get(
    "APPMAP_CLI_JS",
    "/Users/kgilpin/source/appland/appmap-js/packages/cli/built/cli.js",
)
VERIFY_AGENT_SRC = REPO_ROOT / "agents" / "appmap-verify.md"
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
    ap.add_argument("source_codefix_run",
                    help="Path to a try_code_fix.py output dir (must have "
                         "repo/ with the fix applied + rca_report.md).")
    ap.add_argument("--model", default="claude-opus-4-7")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    source = Path(args.source_codefix_run).resolve()
    src_repo = source / "repo"
    rca_path = source / "rca_report.md"
    diff_path = source / "diff.patch"
    if not src_repo.is_dir():
        print(f"no repo/ under {source}", file=sys.stderr); sys.exit(2)
    if not rca_path.exists():
        print(f"no rca_report.md under {source} (was this from try_code_fix?)",
              file=sys.stderr); sys.exit(2)

    rca_report = rca_path.read_text()
    diff = diff_path.read_text() if diff_path.exists() else ""

    fixture_id = source.parent.name
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = REPO_ROOT / "work" / "experiments" / "verify" / fixture_id / ts
    repo = run_dir / "repo"
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"copying {src_repo} → {repo}", flush=True)

    def _skip(srcdir, names):
        if Path(srcdir).resolve() == src_repo:
            return [n for n in names if n in ("tmp", ".claude")]
        return []

    shutil.copytree(src_repo, repo, symlinks=True, ignore=_skip)

    # Restore .mcp.json (we stripped it for Step 2). Verify subagent needs it.
    mcp_src = REPO_ROOT / "work" / "claude-appmap-mcp" / fixture_id
    # Find any prior MCP run for this fixture and reuse its .mcp.json template.
    mcp_template = None
    if mcp_src.is_dir():
        for sub in sorted(mcp_src.iterdir(), reverse=True):
            cand = sub / "repo" / ".mcp.json"
            if cand.exists():
                mcp_template = cand
                break
    if mcp_template is None:
        # Fall back to writing one inline
        import json
        (repo / ".mcp.json").write_text(json.dumps({
            "mcpServers": {
                "appmap": {
                    "command": "node",
                    "args": [APPMAP_CLI_JS, "query", "mcp"],
                }
            }
        }, indent=2))
    else:
        shutil.copy2(mcp_template, repo / ".mcp.json")

    # Drop the verify subagent definition (and rca, in case verify wants
    # to escalate, though that shouldn't happen for a clean fix).
    agents_dir = repo / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(VERIFY_AGENT_SRC, agents_dir / "appmap-verify.md")
    if RCA_AGENT_SRC.exists():
        shutil.copy2(RCA_AGENT_SRC, agents_dir / "appmap-rca.md")
    print(f"  wrote {agents_dir.relative_to(repo)}/", flush=True)

    # Fresh tmp/appmap/ — no prior recordings.
    (repo / "tmp" / "appmap").mkdir(parents=True, exist_ok=True)

    # Override CLAUDE.md for the verify-only scope.
    claude_md = textwrap.dedent("""\
        # verify-only experiment

        A fix has been applied. Your only job: dispatch the
        appmap-verify subagent to confirm the runtime change matches
        the expectation, then print its verdict.

        Do **not** investigate, do not modify any source, do not call
        AppMap tools yourself.

        Steps:
        1. Read `issue.md` for context if you need it.
        2. Dispatch via `Agent(subagent_type="appmap-verify", ...)`,
           passing: the bug summary, the expected runtime change,
           the reproducer command. Tell the subagent there are no
           pre-existing recordings — it must record fresh.
        3. Print the verdict verbatim.
        4. Stop.
        """)
    (repo / "CLAUDE.md").write_text(claude_md)

    if not os.environ.get("DOCKER_HOST"):
        sock = _discover_podman_socket()
        if sock:
            os.environ["DOCKER_HOST"] = sock

    # Seed AppMap index for the new cwd.
    print("seeding AppMap index...", flush=True)
    seed = subprocess.run(
        ["node", APPMAP_CLI_JS, "index"],
        cwd=str(repo), capture_output=True, text=True, check=False,
    )
    sha_match = claude_session.INDEX_DB_RE.search((seed.stdout or "") + (seed.stderr or ""))
    sha = sha_match.group(1) if sha_match else None
    if sha:
        print(f"  index sha: {sha}", flush=True)

    # Start watcher.
    watch_log = (repo / "tmp" / "appmap-watch.log").open("w")
    watcher = subprocess.Popen(
        ["node", APPMAP_CLI_JS, "index", "--watch", "-d", str(repo)],
        cwd=str(repo), stdout=watch_log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    # Compose the main prompt — paper-thin.
    user_prompt = textwrap.dedent(f"""\
        A fix was applied to address the bug in `issue.md`.

        Dispatch the `appmap-verify` subagent to confirm the runtime
        change matches the expected behavior.

        Brief the subagent in the dispatch:
        - The bug: equal-priority exclusive offers stacked because
          `LineOfferConsumer.available()` had an asymmetric
          `a.id < offer.id` tie-breaker. Two exclusive offers with
          equal priority could both consume the same line when added
          in id-descending order.
        - **Expected runtime change:** when two equal-priority
          exclusive offers are present, only one of them should be
          recorded as applied (i.e. `OfferApplications.add` is
          called once, not twice). The id-based asymmetry should be
          gone — the result must not depend on the order vouchers
          are added.
        - **Reproducer:** `bin/run-tests.sh pytest tests/integration/basket/test_repro_oscar_4016.py -v`
          (this test was added by the fix step; it should now pass).
        - There are no pre-existing recordings under `tmp/appmap/`;
          the subagent must record fresh via `bin/record-appmap.sh`.
        - Also run a regression pass:
          `bin/run-tests.sh pytest tests/integration/basket tests/integration/offer tests/integration/voucher`.

        Print the subagent's verdict verbatim. Then stop.
        """)

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
        try:
            os.killpg(watcher.pid, signal.SIGTERM)
            watcher.wait(timeout=5)
        except Exception:
            try:
                os.killpg(watcher.pid, signal.SIGKILL)
            except Exception:
                pass
        live_stop.set()

    (run_dir / "stdout.txt").write_text(out)
    (run_dir / "stderr.txt").write_text(err)
    (run_dir / "rca_report.md").write_text(rca_report)
    (run_dir / "diff.patch").write_text(diff)

    try:
        claude_session.archive_session_logs(repo, run_dir)
    except Exception as e:
        print(f"  warning: archive_session_logs: {e}", flush=True)
    try:
        claude_session.compute_and_save_usage(run_dir, f"verify-{fixture_id}")
    except Exception as e:
        print(f"  warning: compute_and_save_usage: {e}", flush=True)

    print(f"\nrc={rc}")
    if err:
        print("--- stderr (tail) ---")
        print(err[-1000:])
    print(f"run_dir → {run_dir}")


if __name__ == "__main__":
    main()

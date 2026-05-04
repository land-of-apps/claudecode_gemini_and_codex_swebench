"""Drive `claude` and `claude-appmap-*` backends against a synthetic bug fixture.

Two fixture formats are supported:

  v2 (preferred): declarative — a clean upstream tree + a bug.patch +
  a verify.patch. Each run copies the clean upstream, applies bug.patch,
  writes scaffolding, then `git init && git commit -m "."` so the agent
  sees exactly one commit (no leakage of bug history). Verify applies
  verify.patch against the agent's final tree and runs pytest.

  v1 (legacy): mutable bugged_clone where the bug is a committed state
  in a real .git history. Copies the whole tree, agent inherits .git
  and any untracked working-tree files. Retained for fixtures not yet
  converted to v2 — to be removed once all fixtures migrate.

Detection: presence of `clean_upstream` in fixture.json selects v2.

Usage:
  ./venv/bin/python tools/run_synth.py <fixture_id> --backend claude
  ./venv/bin/python tools/run_synth.py <fixture_id> --backend claude-appmap-mcp
  ./venv/bin/python tools/run_synth.py <fixture_id> --backend claude-appmap-3step
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _fast_copy(src: Path, dst: Path) -> None:
    """Clone `src` tree → `dst` using APFS clonefile when available.

    On APFS volumes, `cp -c` triggers clonefile(2) which copy-on-writes
    metadata only — orders of magnitude faster than walking and copying
    every file. Falls back to shutil.copytree for non-Darwin or when
    src/dst are on different volumes (clonefile errors out).

    `dst` must not exist; `dst.parent` is created if necessary.
    """
    src = Path(src)
    dst = Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if platform.system() == "Darwin":
        try:
            subprocess.run(["cp", "-c", "-a", str(src), str(dst)],
                           check=True, capture_output=True)
            return
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    shutil.copytree(src, dst, symlinks=True)


def discover_docker_host() -> str | None:
    if os.environ.get("DOCKER_HOST"):
        return os.environ["DOCKER_HOST"]
    try:
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
    ap.add_argument("fixture_id")
    ap.add_argument("--backend",
                    choices=["claude", "claude-appmap-mcp",
                             "claude-appmap-3step"],
                    required=True)
    ap.add_argument("--model", default="opus-4.7")
    args = ap.parse_args()

    fixture_dir = REPO_ROOT / "synth_bugs" / args.fixture_id
    fx_path = fixture_dir / "fixture.json"
    if not fx_path.is_file():
        print(f"missing fixture: {fx_path}", file=sys.stderr)
        sys.exit(2)
    fx = json.loads(fx_path.read_text())
    issue_md = (fixture_dir / "issue.md").read_text()

    if not os.environ.get("DOCKER_HOST"):
        host = discover_docker_host()
        if host:
            os.environ["DOCKER_HOST"] = host

    # ---- format detection -----------------------------------------------
    is_v2 = "clean_upstream" in fx

    # Per-run dir (resolved before the copy so we can emit log lines).
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = REPO_ROOT / "work" / args.backend / f"synth__{args.fixture_id}" / ts
    repo_dir = run_dir / "repo"
    run_dir.mkdir(parents=True, exist_ok=True)

    if is_v2:
        # ---- v2: clean_upstream + bug.patch + verify.patch --------------
        clean = Path(fx["clean_upstream"]).expanduser().resolve()
        if not clean.is_dir():
            print(f"missing clean_upstream: {clean}", file=sys.stderr)
            sys.exit(2)
        bug_patch_rel = fx.get("bug_patch", f"synth_bugs/{args.fixture_id}/bug.patch")
        bug_patch = (REPO_ROOT / bug_patch_rel).resolve()
        if not bug_patch.is_file():
            print(f"missing bug_patch: {bug_patch}", file=sys.stderr)
            sys.exit(2)

        print(f"copying clean upstream → {repo_dir}", flush=True)
        _t0 = time.time()
        _fast_copy(clean, repo_dir)
        print(f"  copy took {time.time() - _t0:.2f}s", flush=True)

        print(f"applying bug patch: {bug_patch.relative_to(REPO_ROOT)}", flush=True)
        subprocess.run(
            ["patch", "-p1", "-i", str(bug_patch)],
            cwd=str(repo_dir), check=True, capture_output=True,
        )
        # base_commit becomes the synthetic single commit we make below,
        # AFTER scaffolding lands. Stub it for now; resolved later.
        base_commit = ""
    else:
        # ---- v1 (legacy): mutable bugged_clone --------------------------
        bugged = Path(fx["bugged_clone"]).expanduser().resolve()
        bugged_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=bugged, capture_output=True, text=True, check=True,
        ).stdout.strip()
        if bugged_status:
            print(f"\nbugged_clone {bugged} is not clean:\n{bugged_status}",
                  file=sys.stderr)
            print("\nClean it before running:", file=sys.stderr)
            print(f"  git -C {bugged} clean -fdx && "
                  f"git -C {bugged} checkout -- .", file=sys.stderr)
            sys.exit(2)

        base_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=bugged, text=True).strip()
        print(f"copying bugged clone → {repo_dir}", flush=True)
        _t0 = time.time()
        _fast_copy(bugged, repo_dir)
        print(f"  copy took {time.time() - _t0:.2f}s", flush=True)

    instance = {
        "instance_id": f"synth__{args.fixture_id}",
        "repo": "django/django",
        "base_commit": base_commit,
        "problem_statement": issue_md,
        "hints_text": "",
        "version": fx.get("version", "4.1"),
        "patch": "",
        "test_patch": "",
    }

    # Resolve model alias.
    from utils.model_registry import get_model_name
    resolved_model = get_model_name(args.model, args.backend) if args.model else None
    instance["_resolved_model"] = resolved_model

    # Build the per-fixture container wrappers. Both bin/run-tests.sh
    # (no instrumentation) and, for appmap backends, bin/record-appmap.sh
    # (instrumented) — same container, same setup, only the wrap differs.
    import shlex as _shlex, textwrap as _tw

    image = fx["instance_image"]
    mount_path = fx.get("container_mount", "/app")
    container_setup = _tw.dedent(fx.get("container_setup", "") or "")

    def _wrapper_script(extra_inner: str) -> str:
        inner = container_setup + "\n" + extra_inner
        return _tw.dedent(f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ $# -eq 0 ]]; then
              echo "usage: $0 <test-command...>" >&2
              exit 2
            fi
            CLONE="$(cd "$(dirname "$0")/.." && pwd)"
            exec podman run --rm \\
                -v "$CLONE":{_shlex.quote(mount_path)} \\
                -w {_shlex.quote(mount_path)} \\
                -e DATABASE_ENGINE=django.db.backends.sqlite3 \\
                -e DATABASE_NAME=:memory: \\
                -e PYTHONPATH={_shlex.quote(mount_path)}/src \\
                {_shlex.quote(image)} \\
                bash -lc {_shlex.quote(inner)} _wrap "$@"
            """)

    # Compose the agent prompt for this backend.
    base_prompt = (REPO_ROOT / "prompts" / "synth_base_solver.txt").read_text()
    appmap_prompt = (REPO_ROOT / "prompts" / "synth_appmap_mcp_solver.txt").read_text()

    # ---- agent invocation ------------------------------------------------
    # v2: harness writes the per-fixture base scaffolding (issue.md +
    # bin/run-tests.sh) BEFORE the interface's prepare_workspace, so the
    # whole tree (clean upstream + bug + base scaffolding + backend
    # scaffolding) lands in a single git commit.
    # v1: legacy path delegates everything to iface.execute_code_cli (or
    # writes scaffolding inline for vanilla).
    git_env = {**os.environ,
               "GIT_AUTHOR_NAME": "synth", "GIT_AUTHOR_EMAIL": "synth@example.invalid",
               "GIT_COMMITTER_NAME": "synth", "GIT_COMMITTER_EMAIL": "synth@example.invalid"}

    if is_v2:
        # Write per-fixture base scaffolding FIRST, before the interface
        # adds its own backend-specific files.
        (repo_dir / "issue.md").write_text(issue_md)
        bin_dir = repo_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        runt = bin_dir / "run-tests.sh"
        runt.write_text(_wrapper_script('exec "$@"\n'))
        runt.chmod(0o755)

        if args.backend in ("claude-appmap-mcp", "claude-appmap-3step"):
            if args.backend == "claude-appmap-mcp":
                from utils.claude_appmap_mcp_interface import ClaudeAppMapMcpInterface as _AppMapIface
                backend_prompt = appmap_prompt
            else:
                from utils.claude_appmap_3step_interface import ClaudeAppMap3StepInterface as _AppMapIface
                backend_prompt = "(3-step backend builds its own prompts per step)"
            iface = _AppMapIface()
            iface._test_spec = type("S", (), {"instance_image_key": image})()
            iface._ensure_instance_image = lambda inst: print(
                f"  using fixture image: {image}", flush=True)
            if "container_setup" in fx:
                iface._record_script = lambda _inst: _wrapper_script(
                    'exec appmap-python "$@"\n')
                iface._run_tests_script = lambda _inst: _wrapper_script('exec "$@"\n')
            iface._prompt = backend_prompt
            iface.prepare_workspace(repo_dir, instance)
        else:
            from utils.claude_interface import ClaudeCodeInterface
            iface = ClaudeCodeInterface()

        # Single synthetic commit. Message is intentionally `.` so
        # `git log` reveals nothing about the fixture identity.
        subprocess.run(["git", "init", "-q", "-b", "main"],
                       cwd=str(repo_dir), env=git_env, check=True)
        subprocess.run(["git", "add", "-A"],
                       cwd=str(repo_dir), env=git_env, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "."],
                       cwd=str(repo_dir), env=git_env, check=True,
                       capture_output=True)
        base_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_dir), text=True).strip()
        instance["base_commit"] = base_commit

        # Run the agent against the prepared, committed workspace.
        if args.backend in ("claude-appmap-mcp", "claude-appmap-3step"):
            result = iface.run_claude(repo_dir, model=resolved_model,
                                       instance=instance)
        else:
            composed_prompt = base_prompt.format(issue=issue_md)
            result = iface.execute_code_cli(
                prompt=composed_prompt, cwd=str(repo_dir),
                model=resolved_model, instance=instance)
    else:
        # ---- v1 legacy --------------------------------------------------
        if args.backend in ("claude-appmap-mcp", "claude-appmap-3step"):
            if args.backend == "claude-appmap-mcp":
                from utils.claude_appmap_mcp_interface import ClaudeAppMapMcpInterface as _AppMapIface
                backend_prompt = appmap_prompt
            else:
                from utils.claude_appmap_3step_interface import ClaudeAppMap3StepInterface as _AppMapIface
                backend_prompt = "(3-step backend builds its own prompts per step)"
            iface = _AppMapIface()
            iface._test_spec = type("S", (), {"instance_image_key": image})()
            iface._ensure_instance_image = lambda inst: print(
                f"  using fixture image: {image}", flush=True)
            if "container_setup" in fx:
                iface._record_script = lambda _inst: _wrapper_script(
                    'exec appmap-python "$@"\n')
                iface._run_tests_script = lambda _inst: _wrapper_script('exec "$@"\n')
            iface._prompt = backend_prompt
            result = iface.execute_code_cli(
                prompt="", cwd=str(repo_dir), model=resolved_model, instance=instance)
        else:
            from utils.claude_interface import ClaudeCodeInterface
            bin_dir = repo_dir / "bin"
            bin_dir.mkdir(exist_ok=True)
            runt = bin_dir / "run-tests.sh"
            runt.write_text(_wrapper_script('exec "$@"\n'))
            runt.chmod(0o755)
            (repo_dir / "issue.md").write_text(issue_md)
            subprocess.run(["git", "add", "bin/run-tests.sh", "issue.md"],
                           cwd=str(repo_dir), env=git_env, capture_output=True)
            subprocess.run(["git", "commit", "--no-verify", "-m", "vanilla: scaffolding"],
                           cwd=str(repo_dir), env=git_env, capture_output=True)
            base_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=str(repo_dir), text=True).strip()
            composed_prompt = base_prompt.format(issue=issue_md)
            iface = ClaudeCodeInterface()
            result = iface.execute_code_cli(
                prompt=composed_prompt, cwd=str(repo_dir),
                model=resolved_model, instance=instance)

    print(f"\nresult: success={result['success']} rc={result['returncode']}")

    # ---- patch extraction ----------------------------------------------
    if is_v2:
        # v2: tree has exactly one commit (HEAD) which is "clean upstream
        # + bug.patch + scaffolding". Anything in the working tree is the
        # agent's contribution. Use git pathspec exclusions to drop
        # scaffolding edits — the agent shouldn't be touching those, and
        # if they did, they're not part of the fix.
        scaffolding_excludes = [
            ":(top,exclude)bin", ":(top,exclude).mcp.json",
            ":(top,exclude)appmap.yml", ":(top,exclude)issue.md",
            ":(top,exclude)CLAUDE.md", ":(top,exclude).claude",
            ":(top,exclude).gitignore", ":(top,exclude)tmp",
            ":(top,exclude)appmap.log", ":(top,exclude)sandbox/appmap.log",
        ]
        patch = subprocess.check_output(
            ["git", "diff", "HEAD", "--"] + scaffolding_excludes,
            cwd=str(repo_dir), text=True,
        )
    else:
        from utils.patch_extractor import PatchExtractor
        pe = PatchExtractor()
        patch = pe.extract_from_cli_output(result.get("stdout", ""), str(repo_dir),
                                            base_commit=base_commit)
    pred = {
        "instance_id": instance["instance_id"],
        "model_name_or_path": resolved_model or args.model,
        "model_patch": patch,
    }
    (run_dir / "prediction.jsonl").write_text(json.dumps(pred) + "\n")
    pretty = {**pred, "patch_chars": len(patch),
              "patch_files": sum(1 for ln in patch.splitlines() if ln.startswith("diff --git "))}
    (run_dir / "prediction.json").write_text(json.dumps(pretty, indent=2))
    print(f"  prediction → {len(patch):,} chars, {pretty['patch_files']} files")

    # Verify with a hidden test that's NEVER in the agent's repo. Inject it
    # AFTER patch extraction so it doesn't pollute the prediction.
    test_cmd = fx.get("verify_test_command") or fx.get("test_command")
    if is_v2:
        # v2: apply verify.patch against the agent's tree. The patch
        # adds the hidden test file under tests/ — same physical effect
        # as the v1 file copy, but expressed declaratively.
        verify_patch_rel = fx.get("verify_patch",
                                  f"synth_bugs/{args.fixture_id}/verify.patch")
        verify_patch = (REPO_ROOT / verify_patch_rel).resolve()
        if verify_patch.is_file():
            print(f"\n--- applying verify.patch → {verify_patch.relative_to(REPO_ROOT)} ---")
            subprocess.run(
                ["patch", "-p1", "-i", str(verify_patch)],
                cwd=str(repo_dir), check=True, capture_output=True,
            )
        else:
            print(f"\n--- WARNING: missing verify.patch at {verify_patch} ---",
                  file=sys.stderr)
    else:
        verify_src = fx.get("verify_test_src")
        verify_dst = fx.get("verify_test_dst")
        if verify_src and verify_dst:
            src_path = (REPO_ROOT / verify_src).resolve()
            dst_path = (repo_dir / verify_dst).resolve()
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            print(f"\n--- copied hidden verify test → {dst_path} ---")
    print(f"--- verifying with: {test_cmd} ---")
    mount_path = fx.get("container_mount", "/testbed")
    setup_script = fx.get("container_setup",
                           "source /opt/miniconda3/bin/activate testbed\n"
                           "pip install -q -e . >/dev/null 2>&1\n")
    verify_cmd = [
        "podman", "run", "--rm",
        "-v", f"{repo_dir}:{mount_path}", "-w", mount_path,
        "-e", "DATABASE_ENGINE=django.db.backends.sqlite3",
        "-e", "DATABASE_NAME=:memory:",
        # The image bakes in `pip install -e /tmp/repo` so without an
        # override Python imports oscar from /tmp/repo (frozen) instead
        # of /app/src (the agent's edits). Front-load /app/src on
        # PYTHONPATH so the mounted source wins.
        "-e", f"PYTHONPATH={mount_path}/src",
        fx["instance_image"],
        "bash", "-lc",
        setup_script + "\n" + test_cmd + " 2>&1 | tail -15"
    ]
    proc = subprocess.run(verify_cmd, capture_output=True, text=True)
    test_output = proc.stdout
    print(test_output)
    # pytest: "N passed" + no "failed"/"error". unittest: "OK" + no "FAIL".
    pytest_pass = ("passed" in test_output and "failed" not in test_output
                   and "error" not in test_output.lower().split("warnings")[0])
    unittest_pass = "OK" in test_output and "FAIL" not in test_output
    passed = pytest_pass or unittest_pass
    verdict = {
        "fixture_id": args.fixture_id,
        "backend": args.backend,
        "model": resolved_model or args.model,
        "test_command": test_cmd,
        "test_output_tail": test_output,
        "test_passed": passed,
        "patch_chars": len(patch),
        "patch_files_touched": [
            ln.split()[3].lstrip("b/") for ln in patch.splitlines()
            if ln.startswith("diff --git ")
        ],
        "expected_fix_files": fx.get("fix_files", []),
        "fixed_correct_file": any(
            f in {ln.split()[3].lstrip("b/") for ln in patch.splitlines() if ln.startswith("diff --git ")}
            for f in fx.get("fix_files", [])
        ),
    }
    (run_dir / "synth_verdict.json").write_text(json.dumps(verdict, indent=2))
    print(f"\nVERDICT  passed={passed}  correct_file={verdict['fixed_correct_file']}")
    print(f"  → {run_dir}/synth_verdict.json")


if __name__ == "__main__":
    main()

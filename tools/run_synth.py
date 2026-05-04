"""Drive both `claude` and `claude-appmap` against a synthetic bug fixture.

A "synthetic bug" is a Django repo where we've planted a bug + a failing
test, plus a symptom-only bug report (issue.md). This script wires it
through the existing interfaces and verifies the fix by re-running the
failing test in the SWE-bench eval container.

Usage:
  ./venv/bin/python tools/run_synth.py <fixture_id> --backend claude
  ./venv/bin/python tools/run_synth.py <fixture_id> --backend claude-appmap

Fixture layout under synth_bugs/<id>/:
  issue.md          — symptom-only bug report (the prompt)
  fixture.json      — { "bugged_clone": <path>, "instance_image": <ref>,
                        "test_command": "...", "fix_files": [...] }
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

    # Build a synthetic SWE-bench instance dict. base_commit = the planted
    # commit so PatchExtractor's filter (drop files not in base) keeps the
    # agent's edits to django/* and to our planted test file.
    bugged = Path(fx["bugged_clone"]).expanduser().resolve()
    base_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=bugged, text=True).strip()
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

    # Per-run dir.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = REPO_ROOT / "work" / args.backend / instance["instance_id"] / ts
    repo_dir = run_dir / "repo"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"copying bugged clone → {repo_dir}", flush=True)
    _t0 = time.time()
    _fast_copy(bugged, repo_dir)
    print(f"  copy took {time.time() - _t0:.2f}s", flush=True)

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

    # Pick the interface.
    if args.backend in ("claude-appmap-mcp", "claude-appmap-3step"):
        if args.backend == "claude-appmap-mcp":
            from utils.claude_appmap_mcp_interface import ClaudeAppMapMcpInterface as _AppMapIface
            backend_prompt = appmap_prompt
        else:
            # claude-appmap-3step builds its own per-step prompts; the
            # base prompt here is unused (interface ignores _prompt for
            # the 3-step flow), but we set something for diagnostic logs.
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

        # The interface's built-in prompt is replaced with the parity prompt.
        # The agent reads issue.md from the workspace (interface writes it).
        iface._prompt = backend_prompt
        result = iface.execute_code_cli(
            prompt="", cwd=str(repo_dir), model=resolved_model, instance=instance)
    else:
        from utils.claude_interface import ClaudeCodeInterface

        # Vanilla: write the bin/run-tests.sh wrapper into the repo manually
        # (the appmap interface does this itself).
        bin_dir = repo_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        runt = bin_dir / "run-tests.sh"
        runt.write_text(_wrapper_script('exec "$@"\n'))
        runt.chmod(0o755)
        # Also write the issue.md so the prompt can refer to it consistently.
        (repo_dir / "issue.md").write_text(issue_md)
        # Commit the scaffolding so it doesn't pollute the patch.
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "synth", "GIT_AUTHOR_EMAIL": "synth@example.invalid",
               "GIT_COMMITTER_NAME": "synth", "GIT_COMMITTER_EMAIL": "synth@example.invalid"}
        subprocess.run(["git", "add", "bin/run-tests.sh", "issue.md"],
                       cwd=str(repo_dir), env=env, capture_output=True)
        subprocess.run(["git", "commit", "--no-verify", "-m", "vanilla: scaffolding"],
                       cwd=str(repo_dir), env=env, capture_output=True)

        # Re-resolve base_commit since we just committed.
        base_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_dir), text=True).strip()

        composed_prompt = base_prompt.format(issue=issue_md)
        iface = ClaudeCodeInterface()
        result = iface.execute_code_cli(
            prompt=composed_prompt, cwd=str(repo_dir),
            model=resolved_model, instance=instance)

    print(f"\nresult: success={result['success']} rc={result['returncode']}")

    # Extract patch and persist prediction.jsonl.
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

    # Verify with a hidden test that's NEVER in the agent's repo. Copy it in
    # AFTER patch extraction so it doesn't pollute the prediction.
    test_cmd = fx.get("verify_test_command") or fx.get("test_command")
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

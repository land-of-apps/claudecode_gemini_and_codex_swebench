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

    # ---- format detection -----------------------------------------------
    is_v2 = "clean_upstream" in fx
    language = fx.get("language", "python")
    is_java = (language == "java")

    # Container plumbing only matters for the python flow. Java runs
    # host-side via the project's gradle wrapper — no podman, no DOCKER_HOST.
    if not is_java and not os.environ.get("DOCKER_HOST"):
        host = discover_docker_host()
        if host:
            os.environ["DOCKER_HOST"] = host

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

    # Build the per-fixture wrappers — bin/run-tests.sh (no instrumentation)
    # and bin/record-appmap.sh (instrumented). Two language flavors:
    #
    #   python: each wrapper is a `podman run` against a SWE-bench-style
    #           container image, with the agent's clone bind-mounted in.
    #   java:   each wrapper is a host-side `./gradlew` invocation against
    #           the project's gradle wrapper. -Pappmap_enabled=true on
    #           record-appmap.sh activates the AppMap Gradle plugin which
    #           emits .appmap.json under tmp/appmap/junit/.
    #
    # The interfaces (claude_appmap_mcp_interface, claude_appmap_3step_
    # interface) call _record_script and _run_tests_script when laying
    # down scaffolding; we override both via the iface._record_script /
    # _run_tests_script lambdas below.
    import shlex as _shlex, textwrap as _tw

    image = fx.get("instance_image")  # python only; None for java
    mount_path = fx.get("container_mount", "/app")
    container_setup = _tw.dedent(fx.get("container_setup", "") or "")

    if is_java:
        # Init script that pre-attaches the AppMap javaagent to all Test
        # tasks. The plugin's own attachment runs in `appmap` task's
        # doLast which is SKIPPED when any test fails — exactly the case
        # we care about (bug-reproducing test must fail, otherwise the
        # bug isn't planted). See tools/appmap-java-init.gradle for
        # full notes.
        appmap_init = REPO_ROOT / "tools" / "appmap-java-init.gradle"

        def _wrapper_script(record: bool) -> str:
            if record:
                # --no-configuration-cache: the AppMap plugin's extension
                # holds a java.util.logging.Logger that fails to serialize
                # into Gradle's config cache.
                # --init-script: pre-attaches the javaagent (see notes above).
                # -Pappmap_enabled=true: omnibank's build.gradle.kts only
                # applies the plugin when this flag is set.
                # Recordings land at <subproject>/tmp/appmap/junit/*.appmap.json;
                # the post-step copies them into <rootDir>/tmp/appmap/junit/
                # (the canonical location the host-side watcher monitors).
                return _tw.dedent(f"""\
                    #!/usr/bin/env bash
                    set -uo pipefail
                    if [[ $# -eq 0 ]]; then
                      echo "usage: $0 <gradle-task>..." >&2
                      exit 2
                    fi
                    CLONE="$(cd "$(dirname "$0")/.." && pwd)"
                    cd "$CLONE"
                    ./gradlew \\
                        --no-configuration-cache \\
                        --init-script {_shlex.quote(str(appmap_init))} \\
                        -Pappmap_enabled=true \\
                        "$@"
                    status=$?
                    mkdir -p tmp/appmap/junit
                    find . -path '*/tmp/appmap/*.appmap.json' \\
                        ! -path './tmp/appmap/*' -print0 2>/dev/null \\
                        | xargs -0 -I {{}} cp -p {{}} tmp/appmap/junit/ 2>/dev/null || true
                    exit $status
                    """)
            return _tw.dedent("""\
                #!/usr/bin/env bash
                set -euo pipefail
                if [[ $# -eq 0 ]]; then
                  echo "usage: $0 <gradle-task>..." >&2
                  exit 2
                fi
                CLONE="$(cd "$(dirname "$0")/.." && pwd)"
                cd "$CLONE"
                exec ./gradlew "$@"
                """)
    else:
        def _wrapper_script(record: bool) -> str:
            inner = container_setup + "\n"
            inner += 'exec appmap-python "$@"\n' if record else 'exec "$@"\n'
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
        runt.write_text(_wrapper_script(record=False))
        runt.chmod(0o755)

        if args.backend in ("claude-appmap-mcp", "claude-appmap-3step"):
            if args.backend == "claude-appmap-mcp":
                from utils.claude_appmap_mcp_interface import ClaudeAppMapMcpInterface as _AppMapIface
                backend_prompt = appmap_prompt
            else:
                from utils.claude_appmap_3step_interface import ClaudeAppMap3StepInterface as _AppMapIface
                backend_prompt = "(3-step backend builds its own prompts per step)"
            iface = _AppMapIface()
            # _test_spec.instance_image_key is the str the python flow
            # uses to construct podman commands; for java we set a
            # marker that's only ever surfaced in log lines.
            iface._test_spec = type("S", (), {
                "instance_image_key": image if image else f"host-java ({language})",
            })()
            if is_java:
                iface._ensure_instance_image = lambda inst: print(
                    "  using host-side gradle (no container)", flush=True)
            else:
                iface._ensure_instance_image = lambda inst: print(
                    f"  using fixture image: {image}", flush=True)
            # For both python (container_setup present) and java we
            # override the per-instance scripts to use _wrapper_script,
            # which produces the right shape for the language.
            if is_java or "container_setup" in fx:
                iface._record_script = lambda _inst: _wrapper_script(record=True)
                iface._run_tests_script = lambda _inst: _wrapper_script(record=False)
            iface._prompt = backend_prompt
            iface.prepare_workspace(repo_dir, instance)
        else:
            from utils.claude_interface import ClaudeCodeInterface
            iface = ClaudeCodeInterface()

        # Single synthetic commit. Message is intentionally `.` so
        # `git log` reveals nothing about the fixture identity.
        subprocess.run(["git", "init", "-q", "-b", "main"],
                       cwd=str(repo_dir), env=git_env, check=True)
        # AppMap's java agent calls GitUtil.getRepositoryURL() during
        # metadata write and does an unguarded `list.get(0)` on the
        # remote list. With no remote configured the test crashes mid-
        # recording (java.lang.IndexOutOfBoundsException) and the
        # whole gradle build fails. A placeholder local remote makes
        # the list non-empty without leaking any external URL.
        subprocess.run(
            ["git", "remote", "add", "origin",
             "file:///dev/null/synth.git"],
            cwd=str(repo_dir), env=git_env, check=True,
            capture_output=True,
        )
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
                iface._record_script = lambda _inst: _wrapper_script(record=True)
                iface._run_tests_script = lambda _inst: _wrapper_script(record=False)
            iface._prompt = backend_prompt
            result = iface.execute_code_cli(
                prompt="", cwd=str(repo_dir), model=resolved_model, instance=instance)
        else:
            from utils.claude_interface import ClaudeCodeInterface
            bin_dir = repo_dir / "bin"
            bin_dir.mkdir(exist_ok=True)
            runt = bin_dir / "run-tests.sh"
            runt.write_text(_wrapper_script(record=False))
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
            # Revert agent edits to any file verify.patch touches.
            # The agent may have added its own test method whose name
            # collides with the hidden test (causing a duplicate-
            # definition compile error in Java, or a redefinition
            # warning in Python). The agent's PRODUCTION edits live in
            # other files and are preserved by this reset.
            verify_targets = []
            for line in verify_patch.read_text().splitlines():
                if line.startswith("+++ b/"):
                    verify_targets.append(line[len("+++ b/"):].strip())
            reverted, deleted = [], []
            for rel in verify_targets:
                # Two collision cases the agent can produce:
                # 1. verify.patch MODIFIES an existing file (the file is
                #    in HEAD). The agent may have edited the same file
                #    (e.g. added a test method whose name collides with
                #    the hidden one). Reset to HEAD before applying.
                # 2. verify.patch CREATES a new file (not in HEAD). The
                #    agent may have invented a test file at the same
                #    path. `patch -p1` appends to existing files when
                #    the diff says "new file" but the file is present —
                #    producing malformed source with two package
                #    declarations. Delete the agent's invention so the
                #    patch creates a clean new file.
                ls = subprocess.run(
                    ["git", "ls-tree", "--name-only", "HEAD", "--", rel],
                    cwd=str(repo_dir), capture_output=True, text=True,
                )
                full = repo_dir / rel
                if ls.stdout.strip() == rel:
                    subprocess.run(
                        ["git", "checkout", "HEAD", "--", rel],
                        cwd=str(repo_dir), check=True, capture_output=True,
                    )
                    reverted.append(rel)
                elif full.is_file():
                    full.unlink()
                    deleted.append(rel)
            if reverted:
                print(f"\n--- reset agent edits to verify-patch targets: {reverted} ---")
            if deleted:
                print(f"\n--- deleted agent-created files at verify-patch new-file paths: {deleted} ---")
            print(f"--- applying verify.patch → {verify_patch.relative_to(REPO_ROOT)} ---")
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
    if is_java:
        # Host-side: invoke the project's gradle wrapper directly from
        # the agent's repo. The fixture's verify_test_command is a
        # gradle invocation like `./gradlew :module:test --tests
        # 'Class.method'`, so we run it from repo_dir as cwd.
        verify_cmd = ["bash", "-lc", f"{test_cmd} 2>&1 | tail -40"]
        proc = subprocess.run(
            verify_cmd, cwd=str(repo_dir),
            capture_output=True, text=True,
        )
    else:
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

    # Detect pass/fail.
    #   pytest:   LAST line of the form "==== 1 passed in 4.03s ====". A
    #             plain substring match is too eager — a test named
    #             `test_payment_error_branch_thaws_frozen_basket` would
    #             plant the word "error" in unrelated banners.
    #   unittest: "OK" present, "FAIL" absent.
    #   gradle:   "BUILD SUCCESSFUL" present, "FAILED" absent (gradle
    #             prints "BUILD FAILED" + "> Task :foo:test FAILED" on
    #             test failure).
    import re as _re
    summary_lines = _re.findall(
        r'^=+\s+(.+?)\s+=+\s*$', test_output, flags=_re.MULTILINE,
    )
    final_summary = summary_lines[-1] if summary_lines else ''
    pytest_pass = (
        'passed' in final_summary
        and 'failed' not in final_summary
        and 'error' not in final_summary
    )
    unittest_pass = "OK" in test_output and "FAIL" not in test_output
    gradle_pass = (
        "BUILD SUCCESSFUL" in test_output
        and "BUILD FAILED" not in test_output
        and "FAILED" not in test_output
    ) if is_java else False
    passed = pytest_pass or unittest_pass or gradle_pass
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

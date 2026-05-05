"""Convert an omnibank bug branch into a v2 synth_bugs fixture.

Each omnibank bug lives on a `bug/<BUG-ID>/break` branch with a
canonical 3-commit shape:

    <regression>      ← HEAD; introduces the buggy code change
    <test-add>        ← HEAD~1; adds the hidden verify test method
    <base>            ← HEAD~2 (or further); pre-bug, pre-test source

This tool extracts:

  - clean_upstream  = `git archive <base>` extracted to a snapshot dir
  - bug.patch       = diff(<test-add>, <regression>) — the regression
  - verify.patch    = diff(<base>,     <test-add>)   — the hidden test
  - fixture.json    = v2 fixture with language=java, host-side gradle
                      verify_test_command derived from the test file

The resulting fixture lives under `synth_bugs/omnibank_<bug-id-lower>/`
and integrates with the existing run_synth.py v2 flow once the
language=java path is wired (still TODO in run_synth.py at write time).

Usage:
  ./venv/bin/python tools/import_omnibank_bug.py BUG-0001
  ./venv/bin/python tools/import_omnibank_bug.py BUG-0001 --force
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OMNIBANK = Path("/Users/kgilpin/source/land-of-apps/omnibank-demo")
DEFAULT_SNAPSHOT_ROOT = Path.home() / "tmp" / "omnibank_snapshots"


def git(*args, cwd: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(cwd), *args], text=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("bug_id", help="e.g. BUG-0001")
    ap.add_argument("--omnibank", default=str(DEFAULT_OMNIBANK),
                    help=f"omnibank checkout root (default: {DEFAULT_OMNIBANK})")
    ap.add_argument("--snapshot-root", default=str(DEFAULT_SNAPSHOT_ROOT),
                    help=f"where to extract the clean_upstream snapshot "
                         f"(default: {DEFAULT_SNAPSHOT_ROOT})")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing snapshot or fixture")
    args = ap.parse_args()

    bug_id = args.bug_id
    omnibank = Path(args.omnibank).expanduser().resolve()
    snapshot_root = Path(args.snapshot_root).expanduser().resolve()

    if not (omnibank / ".git").exists():
        sys.exit(f"omnibank path is not a git repo: {omnibank}")

    # ---- locate the three commits on bug/<id>/break --------------------
    break_ref = _resolve_break_ref(omnibank, bug_id)
    log = git("log", "--format=%H %s", "-n", "10", break_ref, cwd=omnibank).splitlines()
    if len(log) < 3:
        sys.exit(f"branch {break_ref} has only {len(log)} commits; "
                 "need at least 3 (regression + test-add + base).")

    regression_sha, regression_msg = log[0].split(" ", 1)
    test_add_sha, test_add_msg = log[1].split(" ", 1)
    base_sha, base_msg = log[2].split(" ", 1)

    print(f"  regression commit: {regression_sha[:8]} — {regression_msg}")
    print(f"  test-add commit:   {test_add_sha[:8]} — {test_add_msg}")
    print(f"  base commit:       {base_sha[:8]} — {base_msg}")

    if not regression_msg.startswith(f"{bug_id}: regression"):
        print(f"  WARNING: regression commit message does not start with "
              f"'{bug_id}: regression' — review carefully.", file=sys.stderr)
    if "hidden" not in test_add_msg.lower() and "test" not in test_add_msg.lower():
        print(f"  WARNING: test-add commit message looks unusual — review.",
              file=sys.stderr)
    # Hard fail on mis-stacked branches. omnibank's BUG-0002, for
    # example, is a regression on top of BUG-0001's test-add — there
    # is no BUG-0002-specific hidden test. Without this check the
    # importer pairs the wrong bug with the wrong test (Money.java
    # regression + AchCutoffPolicyTest test) and produces a fixture
    # that cannot fail-then-pass meaningfully.
    if not test_add_msg.startswith(f"{bug_id}:"):
        sys.exit(
            f"ERROR: test-add commit '{test_add_msg}' does not belong to "
            f"{bug_id}. The bug branch appears mis-stacked — there is no "
            f"{bug_id}-specific hidden test commit on `bug/{bug_id}/break`. "
            f"Skip this bug or author a hidden test commit upstream first."
        )

    # ---- generate the patches ------------------------------------------
    bug_patch = git("diff",
                    f"{test_add_sha}..{regression_sha}",
                    cwd=omnibank)
    verify_patch = git("diff",
                       f"{base_sha}..{test_add_sha}",
                       cwd=omnibank)

    # Optional: capture the gold-standard fix for analysis (diff between
    # the regression commit and the corresponding /fix branch). If /fix
    # exists, save it; if not, skip — gold fix is reference, not used at
    # run time.
    gold_fix_patch = None
    fix_ref = _try_resolve_fix_ref(omnibank, bug_id)
    if fix_ref:
        gold_fix_patch = git("diff",
                             f"{regression_sha}..{fix_ref}",
                             cwd=omnibank)

    # ---- parse test metadata -------------------------------------------
    test_module, test_class, test_method = _parse_test_metadata(verify_patch)
    if not test_class:
        sys.exit(f"could not extract test class from verify.patch — "
                 f"verify_patch was:\n{verify_patch[:500]}")
    print(f"  hidden test:       :{test_module} → {test_class}.{test_method}")

    fix_files = _extract_modified_files(bug_patch, exclude_test_files=True)
    if not fix_files:
        print("  WARNING: bug.patch modifies no non-test files — review.",
              file=sys.stderr)
    print(f"  fix files:         {fix_files}")

    # ---- extract the snapshot ------------------------------------------
    snapshot_dir = snapshot_root / bug_id
    if snapshot_dir.exists():
        if not args.force:
            sys.exit(f"snapshot exists: {snapshot_dir} (use --force)")
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True)
    print(f"  extracting tree:   {base_sha[:8]} → {snapshot_dir}")

    archive_proc = subprocess.Popen(
        ["git", "-C", str(omnibank), "archive", base_sha],
        stdout=subprocess.PIPE,
    )
    subprocess.run(
        ["tar", "-x", "-C", str(snapshot_dir)],
        stdin=archive_proc.stdout, check=True,
    )
    archive_proc.wait()
    if archive_proc.returncode != 0:
        sys.exit("git archive failed")

    # Bug-branch base commits in omnibank predate the build's bump from
    # Java 17 → 21, but the source uses Java 21 features (record patterns,
    # qualified type patterns in switch). Without bumping, shared-domain
    # fails to compile against the snapshot's stale toolchain. The fix is
    # one line in build.gradle.kts. (Long-term solution: rebase bug
    # branches onto current main, or rewrite this importer to overlay
    # main's build.gradle.kts on top of the bug-branch source tree.)
    bgk = snapshot_dir / "build.gradle.kts"
    if bgk.is_file():
        text = bgk.read_text()
        bumped = text.replace(
            "JavaLanguageVersion.of(17)",
            "JavaLanguageVersion.of(21)",
        )
        if bumped != text:
            bgk.write_text(bumped)
            print(f"  toolchain bump:    Java 17 → Java 21 in {bgk.name}")

    # ---- compile-gate: verify.patch must compile against the snapshot --
    # BUG-0005 shipped a hidden test referencing ConsumerProduct.CHECKING
    # (the enum has CHECKING_BASIC / CHECKING_PREMIUM). The importer
    # silently produced a fixture whose verify step always fails at
    # compileTestJava, masking otherwise-correct agent fixes. This gate
    # applies verify.patch to the snapshot, runs gradle's compileTestJava
    # on the affected module, and refuses to write the fixture if the
    # hidden-test source can't be compiled. The patch is reverse-applied
    # before continuing so the snapshot is back to base + toolchain bump.
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as tf:
        tf.write(verify_patch)
        verify_patch_tmp = Path(tf.name)
    try:
        apply_proc = subprocess.run(
            ["patch", "-p1", "-i", str(verify_patch_tmp)],
            cwd=str(snapshot_dir), capture_output=True, text=True,
        )
        if apply_proc.returncode != 0:
            sys.exit(
                f"compile-gate: verify.patch failed to apply to snapshot — "
                f"the patch and the base tree are inconsistent.\n"
                f"stdout: {apply_proc.stdout}\nstderr: {apply_proc.stderr}"
            )
        print(f"  compile-gate:      :{test_module}:compileTestJava ...", end="",
              flush=True)
        compile_proc = subprocess.run(
            ["./gradlew", "--no-configuration-cache",
             f":{test_module}:compileTestJava"],
            cwd=str(snapshot_dir), capture_output=True, text=True,
            timeout=600,
        )
        # Always reverse-apply so the snapshot is back to base.
        subprocess.run(
            ["patch", "-R", "-p1", "-i", str(verify_patch_tmp)],
            cwd=str(snapshot_dir), capture_output=True, text=True,
        )
        if compile_proc.returncode != 0:
            print(" FAILED")
            tail = (compile_proc.stdout + "\n" + compile_proc.stderr).splitlines()
            for line in tail[-30:]:
                print(f"    {line}")
            sys.exit(
                f"compile-gate: verify.patch source does NOT compile against "
                f"the snapshot. Refusing to write fixture. Fix the hidden "
                f"test upstream (or in synth_bugs/{bug_id.lower()}/verify.patch "
                f"if you've already imported and just need to patch)."
            )
        print(" OK")
    finally:
        verify_patch_tmp.unlink(missing_ok=True)

    # ---- write the fixture ---------------------------------------------
    fixture_dir = REPO_ROOT / "synth_bugs" / f"omnibank_{bug_id.lower()}"
    if fixture_dir.exists():
        if not args.force:
            sys.exit(f"fixture exists: {fixture_dir} (use --force)")
        # Preserve any hand-authored issue.md if present
        existing_issue = (fixture_dir / "issue.md").read_text() if (fixture_dir / "issue.md").exists() else None
    else:
        existing_issue = None
    fixture_dir.mkdir(exist_ok=True)

    (fixture_dir / "bug.patch").write_text(bug_patch)
    (fixture_dir / "verify.patch").write_text(verify_patch)
    if gold_fix_patch:
        (fixture_dir / "gold_fix.patch").write_text(gold_fix_patch)

    fixture_json = {
        "_format_version": 2,
        "_format_notes": (
            f"v2 fixture imported from omnibank's bug/{bug_id}/break "
            f"branch via tools/import_omnibank_bug.py. language=java, "
            f"host-side Gradle (no container)."
        ),
        "language": "java",
        "clean_upstream": str(snapshot_dir),
        "bug_patch": f"synth_bugs/omnibank_{bug_id.lower()}/bug.patch",
        "verify_patch": f"synth_bugs/omnibank_{bug_id.lower()}/verify.patch",
        "verify_test_command": (
            f"./gradlew :{test_module}:test "
            f"--tests '{test_class}.{test_method}'"
        ),
        "test_module": test_module,
        "test_class": test_class,
        "test_method": test_method,
        "fix_files": fix_files,
        "bug_summary": regression_msg,
        "source_branch": f"refs/remotes/origin/bug/{bug_id}/break",
        "source_commit_regression": regression_sha,
        "source_commit_test_add": test_add_sha,
        "source_commit_base": base_sha,
    }
    (fixture_dir / "fixture.json").write_text(
        json.dumps(fixture_json, indent=2) + "\n",
    )

    if existing_issue is None:
        issue_md = textwrap.dedent(f"""\
            TODO — AUTHOR a user-POV bug report here. Delete everything
            below this TODO line before using this fixture.

            Engineer-POV reference (NOT for the agent — paraphrase as
            customer/operator prose):
              {regression_msg}

            Hidden test that will verify the fix:
              :{test_module} → {test_class}.{test_method}

            Authoring rules:
              - Do NOT name the failing method, class, file, or
                directly-affected layer.
              - Describe what a user observed (a customer, an
                operations engineer reading dashboards, a finance
                reconciliation, an integration partner).
              - Include "steps to reproduce" if the bug has a
                deterministic trigger.
              - Mention what was expected vs what happened.
              - Avoid comparative cues that pin the layer ("X works
                but Y doesn't" — these collapse the symptom-to-
                location gap).

            Reference: see issue.md files in synth_bugs/oscar_4016,
            oscar_n1_basket, etc. for the desired voice.
            """)
        (fixture_dir / "issue.md").write_text(issue_md)

    print(f"\nwrote fixture:        {fixture_dir.relative_to(REPO_ROOT)}/")
    print(f"  bug.patch           {len(bug_patch):,} chars")
    print(f"  verify.patch        {len(verify_patch):,} chars")
    if gold_fix_patch:
        print(f"  gold_fix.patch      {len(gold_fix_patch):,} chars (reference)")
    print(f"  snapshot            {snapshot_dir} ({_count_files(snapshot_dir):,} files)")
    print(f"\nnext steps:")
    print(f"  1. Edit {fixture_dir.relative_to(REPO_ROOT)}/issue.md")
    print(f"     to be user-POV (currently a TODO template).")
    print(f"  2. (TBD) wire run_synth.py to handle language=java fixtures.")


def _resolve_break_ref(omnibank: Path, bug_id: str) -> str:
    candidates = [
        f"refs/remotes/origin/bug/{bug_id}/break",
        f"refs/heads/bug/{bug_id}/break",
        f"bug/{bug_id}/break",
    ]
    for ref in candidates:
        try:
            git("rev-parse", "--verify", ref, cwd=omnibank)
            return ref
        except subprocess.CalledProcessError:
            continue
    sys.exit(f"could not find a 'bug/{bug_id}/break' branch in {omnibank}")


def _try_resolve_fix_ref(omnibank: Path, bug_id: str):
    candidates = [
        f"refs/remotes/origin/bug/{bug_id}/fix",
        f"refs/heads/bug/{bug_id}/fix",
    ]
    for ref in candidates:
        try:
            git("rev-parse", "--verify", ref, cwd=omnibank)
            return ref
        except subprocess.CalledProcessError:
            continue
    return None


def _parse_test_metadata(verify_patch: str):
    """Find the (module, test_class, test_method) added by verify.patch.

    Module = first path segment of the test file. Test class = file
    basename without .java. Test method = first @Test-decorated
    `void name()` in the patch's added lines.
    """
    test_file = None
    for line in verify_patch.splitlines():
        m = re.match(r'^\+\+\+ b/(.+Test\.java)\s*$', line)
        if m:
            test_file = m.group(1)
            break

    if not test_file:
        return None, None, None

    parts = test_file.split('/')
    module = parts[0]
    test_class = parts[-1][:-len(".java")]

    test_method = None
    pending_test_annotation = False
    for line in verify_patch.splitlines():
        if not line.startswith('+'):
            pending_test_annotation = False
            continue
        stripped = line[1:].lstrip()
        if stripped.startswith('@Test'):
            pending_test_annotation = True
            continue
        if pending_test_annotation:
            m = re.match(r'(?:public\s+|protected\s+|private\s+)?void\s+(\w+)\s*\(', stripped)
            if m:
                test_method = m.group(1)
                break
            # If we hit code that isn't a method declaration, drop the flag.
            if stripped and not stripped.startswith(('//', '/*', '*')):
                pending_test_annotation = False

    return module, test_class, test_method


def _extract_modified_files(patch: str, exclude_test_files: bool = False):
    files = []
    for line in patch.splitlines():
        m = re.match(r'^diff --git a/(.+?) b/', line)
        if m:
            f = m.group(1)
            if exclude_test_files and ('Test.java' in f or '/test/' in f):
                continue
            files.append(f)
    return files


def _count_files(d: Path) -> int:
    return sum(1 for _ in d.rglob('*') if _.is_file())


if __name__ == "__main__":
    main()

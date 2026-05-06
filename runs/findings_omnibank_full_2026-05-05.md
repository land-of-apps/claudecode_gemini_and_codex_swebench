# omnibank evaluation report — full sweep, 2026-05-05

All twelve omnibank `bug/BUG-XXXX/break` branches imported and
evaluated against `claude` (vanilla) and `claude-appmap-3step`.
Model: `claude-opus-4-7`.

## Score

| Bug | Vanilla | 3-step | RCA path | Notes |
|-----|---------|--------|----------|-------|
| BUG-0001 | ✅ | ✅ | triage | ACH 16:45 cutoff off-by-one |
| BUG-0002 | — | — | — | Skipped (mis-stacked branch, no BUG-0002 test commit) |
| BUG-0003 | ✅ | ✅ | triage | per-line currency check; required upstream verify-patch relaxation |
| BUG-0004 | ✅ | ✅ | triage | 30/360 Feb-end snap; required harness new-file collision fix |
| BUG-0005 | ✅ | ✅ | triage | hold midnight reset; required upstream `// Bug:` strip + `CHECKING_BASIC` fix |
| BUG-0006 | ✅ | ✅ | triage | loan APPROVED → ACTIVE skipping FUNDED |
| BUG-0007 | ✅ | ✅ | triage | amortization final installment doesn't zero principal |
| BUG-0008 | ⚠ | ⚠ | **full RCA + recording** | concurrent-submit; both correct-shaped, multiple harness/test confounds |
| BUG-0009 | ✅ | ✅ | triage | N+1 query; first eager-style branch (empty bug.patch) |
| BUG-0010 | ✅ | ✅ | triage | wire cutoff lost Fed-holiday awareness |
| BUG-0011 | ✅ | ✅ | triage | Percent precision dropped to 2dp |
| BUG-0012 | ✅ | ✅ | triage | hold expiry-instant boundary exclusive vs inclusive |

**10/11 evaluable bugs pass on both backends.** All wins are via
the RCA's triage path. **One bug (BUG-0008) genuinely required
the full RCA + recording flow**, and is the only bug where the
two backends produced materially different work.

## Recording-pipeline reality

- AppMap-Java recording confirmed working end-to-end through the
  harness on multiple bugs. First evidence in BUG-0003's
  pre-tightening 3-step run: 46 recordings, 92-event largest,
  full classMap, `app: omnibank`, `test_status: succeeded`.
- The recording PATH from the agent's perspective fired only
  once (BUG-0008): RCA chose `report_type: full`, called
  `find_recordings` / `find_calls` / `get_call_tree`, and used
  the runtime evidence to reason about Spring's `@Transactional`
  proxy ordering. That report was sophisticated enough to argue
  the gold fix is itself technically wrong (lock release before
  commit leaks uncommitted state), and proposed a more correct
  outside-`@Transactional` solution.
- Every other omnibank bug we have either named identifiers in
  issue.md, named layers explicitly enough to grep, or had
  supporting Javadoc / sibling-class precedent that made static
  reasoning sufficient. The RCA correctly chose triage on each.

This means: **vanilla Opus 4.7 was strong enough to handle every
omnibank bug except the one that needed runtime reasoning, and on
that one it ran into an unrelated harness confound.** The
recording path adds value precisely on bugs where static
reasoning fails — on this corpus, that's 1-of-11.

## What the harness work shaped

Six harness fixes ranked by how much each one would have skewed
the data if left alone:

1. **MCP iface clobbered project-shipped `appmap.yml`** —
   hardcoded `language: python` overwrote omnibank's `language:
   java + packages: [com.omnibank]` config, producing 0-event
   recordings on every Java fixture before the fix. Iface no
   longer writes `appmap.yml`; the RCA subagent owns it (resets
   to `packages: []` on the full path).
2. **AppMap Gradle plugin's `doLast` attachment skipped on
   failing tests.** Bug-reproducing tests fail by design until
   the patch lands → no agent attached → no recording. Init
   script (`tools/appmap-java-init.gradle`) pre-attaches in
   `afterEvaluate`.
3. **Test-file collision on verify.patch apply.** Two variants:
   (a) agent edits a tracked test file the verify.patch also
   modifies → `git checkout HEAD -- <file>` before apply;
   (b) agent invents a test file at the path verify.patch
   creates new → `unlink()` before apply.
4. **Verdict heuristic missed Gradle's `BUILD SUCCESSFUL`** —
   only checked pytest/unittest banners. Java runs flagged
   passed=False on green builds. Fixed.
5. **`AppMapSerializer` IOOBE on remote-less synth repos.**
   Dummy `file:///dev/null/synth.git` remote unblocks
   `GitUtil.getRepositoryURL()`.
6. **Importer fail-fast for mis-stacked bug branches** (BUG-0002
   has no `BUG-0002:`-prefixed test-add commit).

Two more importer fixes during the bug-by-bug walk:

7. **Compile gate** (`compileTestJava` against snapshot +
   verify.patch) refuses to write a fixture whose hidden test
   doesn't compile. Caught BUG-0005's missing
   `ConsumerProduct.CHECKING` enum value the moment we tried to
   re-import after stripping the `// Bug:` comment upstream.
8. **Patch-generation algorithm rewrite.** Old:
   `bug.patch = diff(test_add..regression)`,
   `verify.patch = diff(base..test_add)`. New:
   `bug.patch = diff(base..break)` minus test files,
   `verify.patch = diff(base..break)` test files only. Correctly
   handles eager-style branches (BUG-0008/9/12) where test-add
   bundles the production fix and the regression undoes only
   the prod side — the snapshot at base IS already the bug
   state, bug.patch is empty.

## Fixture/test-quality issues found and fixed upstream

Three classes of leak / breakage in omnibank's bug branches were
fixed in `omnibank-demo` itself (force-pushed bug branches), so
re-imports are clean:

- **BUG-0003** — verify-patch assertion pinned the literal
  `"MIXED_CURRENCIES"` enum-name string, forcing agents to
  reuse a semantically-conflated existing reason. Both vanilla
  and 3-step legitimately introduced a new `CURRENCY_MISMATCH`.
  Relaxed to `hasMessageContaining("CURRENC")`.
- **BUG-0005** — bug.patch source carried a developer comment
  `// Bug: LocalDate-level isBefore...` that made triage
  trivial; verify.patch hidden test referenced
  `ConsumerProduct.CHECKING` (does not exist; enum has
  `CHECKING_BASIC`/`CHECKING_PREMIUM`). Both fixed.

The other ten bug branches had no leakage (broader survey for
`// Bug:` / `TODO` / `FIXME` / `intentional` / `regression` /
`wrong` / `incorrect` / `broken` in regression-commit diffs).

## BUG-0008 — the interesting one

Worth its own section because it's the only bug where the
backends diverged.

The bug: `PaymentServiceImpl.submit()` lost its `synchronized
(idempotencyKey().intern())` block. Two concurrent submits with
the same idempotency key race past `findByIdempotencyKey`, both
allocate a new PaymentId, both try to save — one wins, one
returns a different PaymentId or trips a unique-key violation.

**Vanilla**: edited PaymentServiceImpl.java to add the
synchronized block back. Then ran `git commit --amend` (or
similar) somewhere in its workflow, leaving `git diff HEAD`
empty. Patch extraction reported `patch_chars: 0`. The verify
ran against HEAD's amended state, which had the fix, so the
test passed by accident. From the verdict's perspective:
`passed: True, correct_file: False, files: []`.

**3-step**: RCA with `report_type: full` recorded the test,
queried via MCP, identified that the gold fix is broken under
Spring's `@Transactional` proxy (lock releases before commit;
leaves a window where another thread inside the lock sees
pre-commit state), and produced a more correct solution: lock
OUTSIDE `@Transactional` via `@Lazy self`-injection. That
required adding a constructor parameter, which broke the
hidden test's `new PaymentServiceImpl(repo, ach, wire, open)`
instantiation — `compileTestJava` failed.

The hidden test is also **timing-flaky** independent of either
backend — a no-op patch passes when JVM scheduling lines the
two threads up sequentially, which is part of why vanilla's
empty extraction still got `passed: True`.

Net data point for the eval:
- 3-step recognized a real production-correctness issue
  (gold-fix-under-@Transactional) that vanilla didn't.
- Neither backend "passed" cleanly on the harness's metric.
- The bug's value to this evaluation is mainly as **harness
  and fixture feedback**, not as a clean head-to-head.

## Open work

- **HEAD-amend bypasses patch extraction.** Aggressive
  vanilla-style agents amend the synthetic commit, ghosting
  their work from `git diff HEAD`. Either detect via
  pre/post `git rev-parse HEAD` comparison, or capture state
  via a separate ref before agent runs.
- **Timing-flaky concurrency tests.** BUG-0008's hidden test
  needs a deterministic interleave (a save-side latch
  releasing only when both threads have passed the lookup),
  not just a `CountDownLatch` on thread start. Worth
  back-porting to omnibank.
- **Review whether omnibank's gold fix for BUG-0008 is
  actually correct under `@Transactional` semantics.** The
  RCA's analysis is plausible; if validated, the fix branch
  may need rework, and the hidden test contract should
  accept either inside-or-outside locking.
- **Author at least one deliberately diffuse fixture** that
  forces the recording path. None of the existing 11 needed
  it, so we have one data point (BUG-0008) for what 3-step
  can do with runtime evidence; more would help.

## Recommendation for future runs

The omnibank corpus as it stands now is a strong evaluation set
for **agent baseline competence on financial-domain bugs** but
gives **little discrimination between strategies**: triage is
sufficient for everything except BUG-0008. To make the corpus
discriminative for the recording-vs-grep question:

- Strip identifying language from issue.md more aggressively.
  Several of the bugs we resolved via triage have issue.md
  that names ACH cutoffs, 30/360 conventions, "approved →
  funded" sequences, etc. — domain language that grep'd
  straight to the file. A more ruthless symptom-only voice
  ("customers report odd interest charges; reproduces on
  contracts ending late February") would force runtime
  exploration.
- Add multi-file bugs. Every omnibank bug is a single-file
  change. Real production bugs often span 2-3 files; triage
  scales worse there.
- Add at least one bug whose call-site is reached only via
  framework dispatch (Spring AOP, message handler), where the
  static call graph hides the actual flow.

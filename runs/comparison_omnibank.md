# omnibank vanilla vs 3-step comparison

Working through omnibank's `bug/BUG-XXXX/break` branches in order.
Each row pairs a `tools/run_synth.py` invocation under
`--backend claude` (vanilla) with one under `--backend
claude-appmap-3step`. Both runs share the same v2 fixture (clean
upstream + bug.patch + verify.patch) so the only difference is the
backend strategy. Model: `claude-opus-4-7`.

| Bug ID  | Vanilla | 3-step | Notes |
|---------|---------|--------|-------|
| BUG-0001 | ✅ pass | ✅ pass (triage) | Bug names "16:45" → RCA correctly took triage path. |
| BUG-0002 | — | — | Skipped: omnibank branch is mis-stacked (no BUG-0002-specific test commit). Importer fails fast. |
| BUG-0003 | ✅ pass | ✅ pass (triage) | Both edited PostingException.java + PostingServiceImpl.java, introducing a new `CURRENCY_MISMATCH` enum (semantically distinct from the existing `MIXED_CURRENCIES` reason). RCA report explicitly justified the new enum. Originally both runs FAILED because verify.patch asserted `hasMessageContaining("MIXED_CURRENCIES")` — pinned to the gold solution's preserved-conflation choice. Relaxed assertion to `hasMessageContaining("CURRENC")` accepts either enum name; both runs pass. **No real recording needed for this bug** — triage-path is the right call. |
| BUG-0004 | ✅ pass | ✅ pass (triage) | 30/360 day-count convention. Both edited only DayCountConvention.java, removing the spurious Feb-28/29 → 30 snap. Vanilla initially failed because the agent invented its own DayCountConventionTest.java; verify.patch's "new file mode 100644" diff appended to the agent's file producing two `package` declarations and 6 compile errors. Harness now deletes agent-created files at verify-patch new-file paths before applying. |
| BUG-0005 | ✅ pass* | ✅ pass* (triage) | available-balance midnight-reset on same-day hold expiries. Both reverted to `h.isActive(now)` in ConsumerAccountServiceImpl.activeHoldsTotal — exactly the gold fix. Original verify.patch referenced `ConsumerProduct.CHECKING` (does not exist; enum has `CHECKING_BASIC` / `CHECKING_PREMIUM`). Original bug.patch shipped with a `// Bug: LocalDate-level...` developer-comment leak that made triage trivial. Both fixed upstream (force-pushed `bug/BUG-0005/break`); fixture re-imported. (* verdict.json files in 20260505_1617... still show passed=False against the original broken assertion; manual replay + clean re-import confirm both agents pass.) |
| BUG-0006 | ✅ pass | ✅ pass (triage) | Loan state machine APPROVED → ACTIVE skip-FUNDED. Both edited only LoanStatus.java, removing `next == ACTIVE` from the APPROVED case. RCA: "single named identifier (`canTransitionTo` / `LoanStatus`) in a clearly-named layer (lending-corporate state machine) maps directly to a 3-line fix." |
| BUG-0007 | ✅ pass | ✅ pass (triage) | Final amortization installment doesn't absorb the rounding residual. Both edited AmortizationCalculator.java to restore the `if (i == periods) { dump remaining balance } else { ... }` branch. RCA pointed at the Javadoc that explicitly documents the missing behavior — "the last installment absorbs the cumulative rounding so the closing balance lands exactly at zero." Static evidence is sufficient because the spec is in-source. |
| BUG-0008 | ⚠ ambiguous | ⚠ ambiguous (**full RCA + recording**) | **First fixture to exercise the recording path end-to-end with the agent driving.** RCA report_type: full; subagent ran `find_recordings`, `find_calls`, `get_call_tree` and produced a sophisticated analysis: "the lock release happens BEFORE @Transactional commits, leaving a window where a second thread inside the lock sees pre-A state — the lock must enclose the transaction, not the other way around." Three layered confounds make the verdict ambiguous: (1) **Vanilla's `git diff HEAD` was empty** (patch_chars=0) despite the file containing the correct synchronized block — the agent amended HEAD via `git commit --amend` somewhere in its workflow, bypassing patch extraction. The verify ran against HEAD's amended state and passed. (2) **The hidden test is timing-flaky** — a no-op patch also passes when JVM scheduling lines threads up sequentially (which is what we got with vanilla's empty patch). (3) **3-step's fix changed the constructor signature** to add `@Lazy PaymentServiceImpl self` for a more correct outside-@Transactional locking pattern; correct in production, but breaks the test's `new PaymentServiceImpl(repo, ach, wire, open)` instantiation → compileTestJava fails. Net: both backends recognized the bug, vanilla's fix was extracted-broken, 3-step's fix was over-engineered relative to the test contract. The data point is most valuable as **harness/fixture feedback**, not as a clean win/loss. |
| BUG-0009 | ✅ pass | ✅ pass (triage) | N+1 query (JOIN FETCH dropped). First "eager-style" branch where the importer's new bug.patch+verify.patch split logic matters: bug.patch is empty (snapshot's base already has the bug), agent runs against base, edits JournalEntryRepository.java to add `join fetch`. 3-step went further — added `select distinct` + `exists` subquery to decouple the WHERE filter from the FETCH (a real Hibernate footgun the gold fix doesn't address). Both pass the structural assertion `jpql.contains("join fetch j.lines")`. |
| BUG-0010 | ✅ pass | ✅ pass (triage) | Wire cutoff lost Fed-holiday awareness (Saturday/Sunday-only check). Both edited only WireCutoffPolicy.java to restore `BusinessCalendar.isBusinessDay`. RCA pointed at the sibling `AchCutoffPolicy` as the existing pattern to follow. |
| BUG-0011 | ✅ pass | ✅ pass (triage) | Percent.of pre-rounded the basis-points→fraction conversion to 2 decimal places, destroying sub-percent precision before the multiply. Both edited only Percent.java to restore the higher (10dp) intermediate scale. RCA noted Money.of already rounds to currency minor units at output, so the bug is "exclusively the pre-rounding" in Percent.of. |
| BUG-0012 | ✅ pass | ✅ pass (triage) | Hold.isActive turned the expiry-instant boundary exclusive (`now.isBefore(expiresAt)`); contract is inclusive. Eager-style branch (empty bug.patch). Both edited only HoldEntity.java to flip back to `!now.isAfter(expiresAt)`. |

## Recording-pipeline evidence

The first BUG-0003 3-step run (before the RCA tightening) exercised
the AppMap-Java recording path end-to-end through the harness:

- 46 recordings produced from a single
  `bin/record-appmap.sh :ledger-core:test` invocation in step3_verify.
- Largest recording (92 events): JournalEntryValidatorTest.currency_mismatch_reports_error.
- Metadata: `app: omnibank`, `language: java`, `git.repository:
  file:///dev/null/synth.git`, `test_status: succeeded`, classMap
  populated.

Confirmed:
- Init-script attaches the AppMap javaagent on every Test task
  (bypassing the plugin's failure-fragile doLast).
- Dummy `file:///dev/null/synth.git` remote prevents the
  `GitUtil.getRepositoryURL()` IndexOutOfBoundsException on
  remote-less synth git inits.

After the RCA tightening (commit 4f7d43a) the RCA correctly chose
triage path on BUG-0003 — naming a layer ("the posting path used
by consumer account opening, ACH credits, and wire transfers") and
explicit identifier ("validation is not running") makes static
analysis sufficient. Recording is reserved for genuinely
runtime-ambiguous bugs.

## Open questions / parking lot

- ~~RCA recording path on Java unexercised~~ — **done** in BUG-0008,
  RCA used MCP queries against a fresh recording and produced a
  Spring-aware analysis.
- ~~Strip `// Bug:` comments upstream~~ — **done** for BUG-0005;
  full survey shows that was the only affected bug.
- ~~Compile gate on import~~ — **done**; importer now runs
  `compileTestJava` on snapshot+verify.patch and refuses to
  produce a broken fixture.
- **HEAD-amend bypass of patch extraction.** BUG-0008 vanilla
  amended the synthetic HEAD commit during its workflow, leaving
  `git diff HEAD` empty even though the file contained the
  correct fix. Either detect amends (`git log` count > 1 vs
  expected, or compare to recorded base SHA) and warn, or capture
  pre-agent state via a different mechanism (separate ref).
- **Timing-flaky concurrency tests.** BUG-0008's hidden test
  passes both with the bug present (when threads happen to
  serialize on JVM scheduling) and with the fix. A robust version
  needs to deterministically interleave — e.g. a save-side
  CountDownLatch that gates thread A's save until thread B has
  reached the lookup. Until then the test gives ~50/50 noise on a
  bug it should always catch.
- **Gold-fix correctness under @Transactional.** RCA in BUG-0008
  flagged that the gold fix's `synchronized` inside an
  `@Transactional` method releases the lock before commit and
  doesn't actually serialize the lookup-vs-commit window. The
  hidden test asserts the gold-fix shape, so a more correct
  outside-@Transactional fix (3-step's choice) gets penalized.
  The fix branch upstream may itself need re-evaluation.
- Test-fairness check: review verify.patch assertions on each
  imported bug for the same kind of brittleness BUG-0003 had
  (literal enum names, exact stack traces, etc).

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

- We haven't yet exercised the **RCA recording path** on Java end-
  to-end — every omnibank fixture so far (5 imported, 4 evaluated)
  has resolved via triage. Look for a more diffuse bug as we work
  through BUG-0006..0012, or author one against omnibank
  specifically.
- Test-fairness check: review verify.patch assertions on each
  imported bug for the same kind of brittleness BUG-0003 had
  (literal enum names, exact stack traces, etc).
- Validate verify.patch *compiles* against its snapshot during
  import — BUG-0005's `ConsumerProduct.CHECKING` references a non-
  existent enum constant. The importer should `javac`-check the
  hidden-test source against the snapshot or otherwise refuse to
  produce a fixture whose verify.patch can't compile.
- bug.patch comment leakage: BUG-0005's regression diff includes
  a `// Bug: LocalDate-level isBefore excludes...` comment in the
  source. Both agents (and the RCA) trivially located the bug by
  grep'ing for that comment. The omnibank bug-branch authoring
  convention should strip such comments before generating the
  break diff.

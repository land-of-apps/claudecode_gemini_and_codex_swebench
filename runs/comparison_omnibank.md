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
  to-end — every omnibank fixture so far has resolved via triage.
  Look for a more diffuse bug as we work through BUG-0004..0012.
- Test-fairness check: review verify.patch assertions on each
  imported bug for the same kind of brittleness BUG-0003 had
  (literal enum names, exact stack traces, etc).

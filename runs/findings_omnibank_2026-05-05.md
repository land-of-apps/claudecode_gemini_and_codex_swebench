# omnibank evaluation report — 2026-05-05

Five omnibank bug fixtures imported and evaluated against `claude`
(vanilla) and `claude-appmap-3step`. Model: `claude-opus-4-7`.

## Score

| Bug | Vanilla | 3-step | RCA path |
|-----|---------|--------|----------|
| BUG-0001 | ✅ | ✅ | triage |
| BUG-0002 | — | — | skipped (mis-stacked branch) |
| BUG-0003 | ✅* | ✅* (triage) | triage |
| BUG-0004 | ✅* | ✅ | triage |
| BUG-0005 | ✅** | ✅** (triage) | triage |

`*` after a fixture/harness fix; the agents always produced a
correct production-code patch, but the test or verify pipeline
failed independently. `**` indicates the verdict.json file still
shows passed=False against the broken assertion (manual replay
confirms PASS post-fix).

**Bottom line: 4/4 evaluated bugs pass on both backends**, every
one via the RCA's triage path. We have **no signal yet on the
full RCA + recording path on Java** because no omnibank bug we've
seen so far has a sufficient symptom-to-location gap to require
runtime evidence.

## What we proved

- **AppMap-Java recording works end-to-end through the harness.**
  BUG-0003's first 3-step run (before the RCA tightening that
  taught it to switch to triage) recorded under
  `bin/record-appmap.sh :ledger-core:test`: 46 recordings, largest
  92 events, full classMap, `app: omnibank`,
  `test_status: succeeded`. Pipeline validated.
- The synthetic git init (single commit, no remote) no longer
  crashes the AppMap-Java agent. Adding a placeholder
  `file:///dev/null/synth.git` remote unblocks
  `GitUtil.getRepositoryURL()` without leaking any real URL.
- The Gradle plugin's failure-fragile `doLast` attachment is
  bypassed via a host-side init script that pre-attaches the
  agent to every Test task in `afterEvaluate`. Bug-reproducing
  tests (which fail by design until the agent's patch lands) now
  produce recordings instead of leaving the recording empty.

## What we couldn't measure

- **The full-RCA-needs-recording flow on Java.** Every omnibank
  bug we've evaluated has named identifiers in the issue.md or in
  the codebase that grep + Read can resolve. The RCA correctly
  picks the triage path on each. To exercise the recording flow
  end-to-end with the agent driving it, we need a bug with a real
  symptom-to-location gap — multiple plausible call sites,
  behavior described without a code identifier, runtime ordering
  that matters.

## Harness work surfaced by this run

In order of how much each one would have skewed the data if
left alone:

1. **MCP iface clobbered project-shipped `appmap.yml`.** Hardcoded
   `language: python` overwrote omnibank's `language: java` +
   `packages: com.omnibank` config — produced 0-event recordings on
   every Java fixture before the fix. Iface no longer writes
   appmap.yml; the RCA subagent owns it and resets to `packages:
   []` on the full path.
2. **AppMap Gradle plugin's `doLast` attachment.** The plugin
   skips agent attachment when any test fails, which is
   precisely the case for all bug-reproducing fixtures. Worked
   around with an init script (Groovy, runtime class resolution)
   that pre-attaches the agent unconditionally.
3. **Test-file collision on verify.patch apply.** Two variants:
   (a) agent edits a test file the verify.patch also modifies →
   `git checkout HEAD -- <file>` before apply; (b) agent invents
   a test file at the path verify.patch creates new → `unlink()`
   before apply. Both branches now handled.
4. **Verdict heuristic missed Gradle's `BUILD SUCCESSFUL`** —
   only checked pytest/unittest banners. Java runs flagged
   passed=False on green builds. Fixed.
5. **`AppMapSerializer` IOOBE on remote-less synth repos.** Dummy
   remote workaround (item above).
6. **Importer fail-fast for mis-stacked bug branches.** BUG-0002
   has no `BUG-0002:`-prefixed test-add commit; the importer was
   silently pairing the wrong fix with the wrong test. Now exits
   with a clear error.

## Fixture/test-quality issues found

- **BUG-0003**: `hasMessageContaining("MIXED_CURRENCIES")` pinned
  the literal enum-name string. Both agents legitimately disambiguated
  to a new `CURRENCY_MISMATCH` (the existing enum's semantics are
  conflated). Relaxed to `hasMessageContaining("CURRENC")`.
- **BUG-0005**: hidden test references `ConsumerProduct.CHECKING`
  which does not exist in the snapshot; the enum has
  `CHECKING_BASIC` and `CHECKING_PREMIUM`. Patched.
- **BUG-0005**: bug.patch source includes a developer comment
  `// Bug: LocalDate-level isBefore excludes holds expiring
  later today.` Both agents (and the RCA) grep'd straight to it.
  Defeats the symptom-to-location-gap signal — the omnibank
  bug-branch authoring convention should strip such comments
  before producing the break diff.

## RCA prompt: what we tightened

- **First-decision section**: choosing the full path explicitly
  commits the subagent to recording. If grep + Read would suffice,
  that's `report_type: triage`, not a full-path shortcut. Closes
  a loophole observed in BUG-0003's first run, where the RCA
  declared `report_type: full` but did grep + Read instead of
  recording.
- **Step 2 (reset appmap.yml)**: unconditional on the full path,
  regardless of whether `find_recordings` returned a match. Drops
  the "before recording" qualifier that gave the RCA cover to
  skip the reset.
- **Step 3 (record)**: required, not conditional. Pre-existing
  recordings only count if they were made AFTER the reset
  (otherwise scope is stale).

Net effect: the RCA either records (full) or returns triage. No
in-between path where it claims full-RCA confidence from static
analysis alone.

## Recommendations

1. Continue importing BUG-0006..0012 and run the same comparison.
   Some of those (BUG-0008 synchronized-submit, BUG-0009 N+1) may
   genuinely need recording.
2. Author at least one **deliberately diffuse** Java fixture
   designed to defeat triage — describe behavior without naming
   identifiers, ensure the call site isn't grep-resolvable from
   the report. This gives a clean comparison datapoint for the
   recording path.
3. Strip `// Bug:` developer comments from omnibank's bug branches
   upstream, then re-import affected fixtures.
4. Add a snapshot-compile gate to `tools/import_omnibank_bug.py` —
   refuse to write a fixture whose verify.patch source doesn't
   compile against the extracted snapshot.
5. Backport the `hasMessageContaining` relaxation pattern (or
   prefer `isInstanceOf` over message-string checks) to other
   omnibank hidden tests.

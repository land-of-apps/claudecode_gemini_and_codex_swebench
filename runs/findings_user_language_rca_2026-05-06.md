# Customer-language bug reports trigger recording-grounded RCA — 2026-05-06

## Question

Across 11/12 omnibank 3-step runs we'd been seeing the RCA subagent
choose the **triage** path (no recording) rather than full RCA. The
triage prompt's gating criterion is "the bug report names a specific
identifier in a named layer" — but the bug reports themselves were
written by an engineer who'd already debugged the bug, often naming
methods, classes, framework concepts, and concurrency primitives a
real user would never know. The criterion was firing on engineer
pre-digestion rather than on actual symptom-to-location ambiguity.

If we rewrite the bug reports as a real customer-care or operations
person would file them — symptom-only, no code names — does the RCA
subagent finally take the full path on bugs that actually need
runtime evidence?

## Setup

Three omnibank fixtures rewritten in operator language:

- **BUG-0006** — corporate loans appearing live without funding.
  Original named "approved → live" transitions; rewrite says "three
  loans where cash never moved on our side, audit trails skip the
  funding step entirely."
- **BUG-0008** — duplicate payment rows on retried submissions.
  Original said "from two threads, simultaneously call submit()
  using a CountDownLatch"; rewrite says "customers hit 'send' twice
  in quick succession, got billed twice, our retry contract is
  supposed to prevent this."
- **BUG-0009** — N+1 in audit portal. Original named JPQL,
  JOIN FETCH, Hibernate, lazy loading; rewrite says "audit portal
  takes 5-15s for accounts with >200 entries, latency scales
  linearly with entry count."

All three rewrites have **0 identifier-shaped tokens** by grep
(camelCase, methodName(), snake_case all stripped).

Each fixture run twice on `claude-appmap-3step` with `--model sonnet`:
once on the original engineer-flavored issue.md, once on the rewrite.

Commit landing the rewrites: `cec2d44`.

## Results

| Bug | Report style | RCA path | RCA recs | Cost | Verdict |
|-----|--------------|----------|----------|------|---------|
| 0006 | engineer | triage | 0 | $1.24 | PASS |
| 0006 | **user-lang** | triage | 0 | $1.10 | PASS |
| 0008 | engineer | triage | 0 | $1.83 | PASS |
| 0008 | **user-lang** | **FULL** | **2** | $2.92 | PASS |
| 0009 | engineer | triage | 0 | $1.11 | PASS |
| 0009 | **user-lang** | triage | 0 | $1.29 | PASS |

(Recording counts above are from step 1 RCA only. Steps 2 and 3 may
record additional `.appmap.json` files for verify; those don't count
toward the RCA-path shift question.)

For reference, Opus baselines on the engineer-flavored reports (from
prior runs): bug-0006 $10.78, bug-0008 $11.81, bug-0009 $6.58 — all
triage. The Opus → Sonnet move was already an 6× cost cut at no
correctness loss; the user-language rewrite layered on top.

## What shifted and what didn't

**Bug-0008 shifted from triage to full RCA.** With the engineer
report telegraphing concurrency primitives, the agent had enough
to grep its way to `submit()` and reason out a per-key lock. With
the symptom-only report, no grep target exists — the agent recorded
the actual flow, queried the call tree, and produced a 3,632-char
analysis with thread IDs and event counts. Cost rose 60% ($1.83 →
$2.92) but the fix is now backed by real runtime evidence.

**Bug-0006 and bug-0009 did NOT shift.** They stayed on the triage
path even with no identifiers in the report. Both are static-code
bugs:

- BUG-0006's symptoms ("loans live without funding event") still
  let the agent grep for `loan` + `state` + `transition` in a Java
  domain model and find a state-machine class. The bug *is* the
  source code being wrong; reading the source is the diagnosis.
- BUG-0009's symptoms ("queries scale per row") plus the codebase
  shape (JPA repositories, JPQL strings) let the agent grep for
  `@Query` annotations and spot a missing `FETCH` keyword. Same
  pattern: the bug lives in the SQL string; reading it is the
  answer.

For both, triage is *appropriate* — the recording would only
confirm what source already shows.

## Interpretation

The triage criterion isn't broken. It's keying on the underlying
**bug nature**, not on the report's specificity. The
report-specificity issue was a separate problem: engineer-flavored
reports cause unnecessary triage of bugs whose nature would
otherwise warrant full RCA.

Three categories of bugs emerge from the omnibank set:

1. **Static-code bugs** — wrong branch in state machine, missing
   prefetch, wrong arithmetic, wrong boundary. Reading the source
   at the right location IS the diagnosis. Triage is correct
   regardless of how the report is written. (BUG-0006, BUG-0009,
   most others.)

2. **Runtime-semantic bugs** — concurrency, transaction boundary,
   AOP proxy, lazy init, cache coherence. The source can look
   identical to a working version; the call tree at runtime is
   the only diagnostic. Need full RCA + recording. (BUG-0008.)

3. **Behavior-only bugs** that telegraph their location via
   primitives in the report (CountDownLatch, JOIN FETCH, etc.) —
   actually category 2 in disguise, miscategorized by the agent
   because the report did the engineering work. Rewriting the
   report as user-language correctly re-categorizes them.

The user-language rewrite is the corrective for category 3. It has
no effect on category 1 (the bug nature still doesn't need
recording) and no effect on category 2 *as observed* (the original
report didn't have the engineering-leak in the first place).

## Implications

**Fixture design**: bug reports should be written from the
perspective of a customer-care ticket or operations escalation,
not from the perspective of an engineer documenting a fix. The
identifier-density grep is a useful sanity check (`grep -E
"[A-Z][a-z]+[A-Z]|[a-z]+\(\)|_"` — should return ≤1 line for a
clean report). Apply this check at fixture-import time.

**The architecture is doing what it claimed to do**, just on a
narrower set of bugs than we'd been measuring. For static-code
bugs (the majority of omnibank), triage + verify gives correct
fixes at $1-2 with Sonnet. For runtime-semantic bugs (one bug in
this set, BUG-0008), the full RCA + recording flow runs at $3 and
produces evidence the agent can't get from grep. Both are wins
versus single-session vanilla.

**Cost story**: if these results hold across more bugs, omnibank
can run end-to-end on Sonnet at ~$1-3 per bug, with full
runtime-grounded RCA on the cases that actually need it.

**Open questions**:
- How many bugs across the omnibank set are category 2 once their
  reports are de-engineered? The full-suite test would be:
  rewrite all 12 reports, re-run on Sonnet, count how many flip
  to full path. If only BUG-0008 shifts, the practical
  recording-needed rate is ~10%. If 3-4 shift, the architecture
  is doing more heavy lifting than we'd realized.
- Vanilla Sonnet baseline. The 3-step Sonnet costs ($1-3) need a
  vanilla Sonnet comparison to know how much of the win is
  Sonnet vs how much is the 3-step structure.

## Artifacts

Run dirs:

- `work/claude-appmap-3step/synth__omnibank_bug-0006/20260506_072648/`
- `work/claude-appmap-3step/synth__omnibank_bug-0008/20260506_072648/`
- `work/claude-appmap-3step/synth__omnibank_bug-0009/20260506_072648/`

Re-run the analysis with:

```
./venv/bin/python tools/rca_breakdown.py \
    work/claude-appmap-3step/synth__omnibank_bug-0006/20260506_072648/ \
    work/claude-appmap-3step/synth__omnibank_bug-0008/20260506_072648/ \
    work/claude-appmap-3step/synth__omnibank_bug-0009/20260506_072648/
```

Bug-report rewrites in commit `cec2d44`.

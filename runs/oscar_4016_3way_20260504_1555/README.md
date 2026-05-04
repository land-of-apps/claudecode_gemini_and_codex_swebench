# Oscar 4016 — 3-way solver comparison (2026-05-04 15:55 UTC)

All three backends launched in parallel against the same fixture
(`synth_bugs/oscar_4016`) on the same machine, model `opus-4.7`.

## Bug

`LineOfferConsumer.available()` in `src/oscar/apps/basket/utils.py`
uses an asymmetric tie-breaker (`a.id < offer.id`) so two exclusive
voucher offers can both apply when added in the right order. Gold
test: `verify_test.py` (lifted from upstream PR #4095) — asserts
`len(filled_basket.offer_applications) == 1` after applying two
exclusive offers.

## Results

| Backend       | Verdict | Files touched                                         | Cost    | Pre-edit (RCA) | Post-edit         |
| ------------- | ------- | ----------------------------------------------------- | ------- | -------------- | ----------------- |
| vanilla       | ❌ FAIL  | `apps/offer/applicator.py`, regression test          | $30.55  | $9.16  (30%)   | $21.39            |
| MCP           | ✅ PASS  | `apps/basket/utils.py` (+ 2 cosmetic label additions) | $55.72  | $9.80  (18%)   | $45.91            |
| 3-step        | ✅ PASS  | `apps/basket/utils.py`                                | $18.82  | $10.30 RCA / $1.50 fix / $7.01 verify  | —                 |

Heuristic split: for single-session backends, "RCA" is everything up
to the assistant's first source-file Edit/Write. For 3-step it's
structural (step1_rca dir + appmap-rca subagent jsonl).

Re-run the analyzer:

    ./venv/bin/python tools/rca_breakdown.py runs/oscar_4016_3way_20260504_1555/{claude,claude-appmap-mcp,claude-appmap-3step}

## Why vanilla failed

It edited `Applicator.get_offers()` to sort offers by
`(-priority, id)` — a workaround that aligns iteration with the
existing asymmetric tie-breaker. The gold test bypasses
`get_offers()` (it calls `apply_offers(offers=[offer2, offer1])`
directly), so the workaround has no effect on the verifier.

This is the cleanest illustration of what runtime evidence buys
you: from grep alone, "the bug is about offer ordering" is a
plausible diagnosis. From a recording with both
`OfferApplications#add` events firing, "the comparison at line
166 is asymmetric" is the only reading.

## Why 3-step beat MCP on cost

MCP single-session burned $45 *after* its first edit — continuous
re-investigation (extra recordings, MCP queries, regression
sweeps, more edits). With AppMap available the whole time the
agent kept re-validating.

3-step's code-fix step has NO MCP at all and is told to just
apply the fix from the RCA report. That phase was $1.50 vs MCP's
$45.91 — a 30× differential for the same operation. Plus 3-step's
verify step ($7) is a *real result* (fresh recording confirming
the buggy call no longer fires) — different in kind from MCP's
$45 of regression sweeps.

## What's in each subdir

```
<backend>/
  console.log              live-mirror of stdout/stderr from claude
  prediction.json/jsonl    final patch in SWE-bench format
  synth_verdict.json       gold-test result + files touched
  usage.json               aggregated cost across parent + subagent sessions
  sessions/
    <session>.jsonl        parent claude session JSONL
    subagents/
      agent-*.jsonl        subagent JSONL (3-step only — RCA + verify)
      agent-*.meta.json    {agentType, description}
  step{1,2,3}_*/           3-step only — per-phase symlinks + console.log
  rca_report.md            3-step only — the RCA subagent's report
  appmap.zip               MCP/3-step only — the recordings the run produced
```

(The `repo/` clone is excluded — too large and reproducible.)

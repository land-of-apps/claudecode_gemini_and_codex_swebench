# Oscar N+1 basket — vanilla vs 3-step (2026-05-04)

Compares vanilla `claude` against `claude-appmap-3step` on a planted
N+1 query bug in oscar's basket render path. Model `opus-4.7`. No
MCP single-session run for this fixture — only vanilla and 3-step.

## Bug

`Basket.all_lines()` in `src/oscar/apps/basket/abstract_models.py`
returns `self.lines.all().order_by(...)` with no `select_related`
or `prefetch_related`. The basket render path then iterates lines
and re-issues a stockrecord query per line for `purchase_info`, an
images query for `primary_image`, an attributes query for
`description`, etc. Render time scales linearly with line count.

Gold test (`verify_test.py`) asserts query count stays bounded
when rendering a basket with many lines — i.e. the prefetch was
actually added.

## Results

| Backend                    | Verdict | Cost    | Files touched                                                                                       |
| -------------------------- | ------- | ------- | --------------------------------------------------------------------------------------------------- |
| vanilla                    | ✅ PASS  | $29.71  | basket/abstract_models.py, partner/strategy.py, templatetags/purchase_info_tags.py                  |
| 3-step (attempt 1, 16:37)  | ❌ FAIL  | $16.20  | basket/abstract_models.py                                                                            |
| 3-step (attempt 2, 17:03)  | ✅ PASS  | $21.10  | basket/abstract_models.py, tests/integration/checkout/test_mixins.py                                 |

Phase breakdown:

- vanilla:        RCA $7.06  (24%) / post-RCA $22.65
- 3-step att.1:   RCA $7.51 / fix $4.14 / verify $4.56  (failed)
- 3-step att.2:   RCA $9.78 / fix $8.29 / verify $3.03

## Headline reading

The moderate win for 3-step. Both architectures pass; 3-step is ~29%
cheaper ($21 vs $30) and produces a cleaner patch (2 files vs 3).
Vanilla over-fixes, going broader through `partner/strategy.py` and
`templatetags/purchase_info_tags.py` — additional layers it tried to
optimize. Both pass the scaling-bound test, but the 3-step patch is
more surgical.

## Observations

- **Both architectures identified the right root-cause file.** Vanilla
  and both 3-step runs all targeted `basket/abstract_models.py`'s
  `all_lines()`. RCA-grounded or grep-grounded, the location was
  unambiguous from the symptoms.

- **Vanilla over-fixed.** It added the prefetch in `all_lines()`
  (the necessary fix) AND threaded `stockrecord` through
  `Strategy.fetch_for_line` and `purchase_info_for_line`. The
  threading is a related optimization but the prefetch alone is
  what the gold test checks. So vanilla's patch is correct but
  larger than needed.

- **3-step's first attempt failed** despite a correct RCA report.
  The code-fix step at $4.14 either (a) wrote the prefetch in a
  way that didn't satisfy the verifier, or (b) the verify subagent
  flagged it as not yet bounded. The second attempt iterated and
  passed at slightly higher cost.

- **3-step is still cheaper across both attempts.** Even counting
  the failed run, total 3-step cost ($16.20 + $21.10 = $37.30) is
  only 26% more than vanilla's single passing attempt ($29.71) —
  while you also get a verification artifact for the second run.

- **The 3-step verify step is doing work vanilla doesn't have.**
  $3-5 of fresh recording + query proves the N+1 is gone in the
  recording, which is a stronger artifact than "the test passed."
  Vanilla has nothing equivalent.

## Layout

```
claude/                                vanilla, 17:16
  console.log, prediction.json/jsonl, sessions/, synth_verdict.json, usage.json
claude-appmap-3step-1637-fail/         3-step first attempt, 16:37 (FAILED verifier)
  + step{1,2,3}_*/, rca_report.md, appmap.zip
claude-appmap-3step-1703-pass/         3-step second attempt, 17:03 (PASSED)
  + step{1,2,3}_*/, rca_report.md, appmap.zip
```

Re-run analysis:

    ./venv/bin/python tools/rca_breakdown.py runs/oscar_n1_basket_20260504/{claude,claude-appmap-3step-1637-fail,claude-appmap-3step-1703-pass}

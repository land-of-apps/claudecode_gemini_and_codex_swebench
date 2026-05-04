# Oscar duplicate order email — vanilla vs 3-step (2026-05-04)

A planted bug in `src/oscar/apps/checkout/mixins.py` where
`OrderPlacementMixin.handle_successful_order` calls
`self.send_order_placed_email(order)` twice in succession — once
in a try/except wrapper added with the comment "Send the confirmation
message early...", and again unconditionally below. Result: two
emails sent per successful checkout.

Gold test (`verify_test.py`) asserts exactly one email lands in
`mail.outbox` after a successful checkout.

## Headline reading

The surprise. Vanilla is **2.6× cheaper** ($3.23 vs $8.55) and
**3.5× faster** (1.7 min vs 6.0 min). The duplicate-email bug is
straightforwardly greppable once you read `checkout/mixins.py` —
two consecutive `self.send_order_placed_email(order)` calls in
`handle_successful_order`, that's the bug. No runtime trace
needed. Vanilla converged in 28 messages. The 3-step's
three-claude-session overhead is real architectural cost (RCA
round-trip + verify round-trip) that doesn't pay off when the bug
yields to grep.

## Results

| Backend  | Verdict | Cost   | Wall  | Files                  |
| -------- | ------- | ------ | ----- | ---------------------- |
| vanilla  | ✅ PASS  | $3.23  | 1.7 min | `checkout/mixins.py` |
| 3-step   | ✅ PASS  | $8.55  | 6.0 min | `checkout/mixins.py` |

Phase breakdown:

- vanilla:  RCA $2.18 (67%) / post-RCA $1.05 — found the duplicated
            call almost immediately by reading the file.
- 3-step:   RCA $3.08 / fix $3.33 / verify $2.14 — RCA recorded the
            checkout flow, queried for `send_order_placed_email`
            calls, found two events in the call tree. Verify
            re-recorded after the fix and confirmed only one event.

## Observations

- **Both fixes are identical in effect.** Vanilla and 3-step both
  removed the new try/except block (lines 246-253) and kept the
  original unconditional send at line 256 — same patch, different
  wall time and cost.

- **3-step's verify step is still doing real work.** A fresh
  recording of the checkout flow proves only one
  `send_order_placed_email` event fires after the fix. That's a
  stronger artifact than "the test passed" — but at $2.14 + 6 min
  wall time, it's expensive insurance against a bug class that
  vanilla solved in under 2 minutes for $1 less than the verify
  step alone.

- **Where the architectural overhead shows up.** 3-step's RCA
  step took 33 messages to produce its report; vanilla's whole
  run was 28 messages. For a grep-class bug, the
  recording+query+report cycle is structurally heavier than just
  reading the file.

- **3-step doesn't get cheaper because the bug is easy.** Even
  on a one-line bug, the framework's fixed costs (record the
  flow, query the call tree, write the report, dispatch the
  verify subagent, re-record) impose a floor. The advantage of
  the 3-step architecture is consistency on harder bugs, not
  speed on easier ones.

## Layout

```
claude/                        vanilla, 17:32
  console.log, prediction.json/jsonl, sessions/, synth_verdict.json, usage.json
claude-appmap-3step/           3-step, 16:49
  + step{1,2,3}_*/, rca_report.md, appmap.zip
```

Re-run analysis:

    ./venv/bin/python tools/rca_breakdown.py runs/oscar_dup_order_email_20260504/{claude,claude-appmap-3step}

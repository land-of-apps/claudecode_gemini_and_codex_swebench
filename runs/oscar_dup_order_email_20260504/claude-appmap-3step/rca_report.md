Confirmed. `handle_successful_order` (event 5871) calls `send_order_placed_email` twice as direct children (events 5872 and 5968), each in turn invoking `OrderDispatcher#send_order_placed_email_for_user`. Both calls happen within the single checkout request. Evidence is conclusive.

## Root cause

`OrderPlacementMixin.handle_successful_order` in `src/oscar/apps/checkout/mixins.py` invokes `self.send_order_placed_email(order)` twice in succession — once at line 251 (inside a try/except wrapper added with the comment "Send the confirmation message early...") and again unconditionally at line 256. Each invocation constructs a fresh `OrderDispatcher` and calls `send_order_placed_email_for_user`, producing two distinct SMTP/LocMem sends per single successful checkout. The runtime call tree shows this clearly: a single `handle_successful_order` (event 5871) has two child `send_order_placed_email` events (5872 and 5968), each fanning out to its own `OrderDispatcher#send_order_placed_email_for_user` (events 5875 and 5971).

## Evidence

- Recording: `Thank you view custumers can reach the thank you page` — single checkout request through `PaymentDetailsView.submit -> handle_order_placement -> handle_successful_order`.
- Call path: `OrderPlacementMixin#handle_successful_order` (parent event 5871) -> `OrderPlacementMixin#send_order_placed_email` invoked twice (events 5872 at depth 3, then 5968 at depth 3) at `src/oscar/apps/checkout/mixins.py:276`.
  - Each child further invokes `OrderDispatcher#send_order_placed_email_for_user` at `src/oscar/apps/order/utils.py:310` (events 5875 and 5971), confirming two independent SEND events.
- `find_calls --method=send_order_placed_email` returns exactly two rows for the same recording, matching the two-email symptom in the bug report.

## Files / lines

- `/Users/kgilpin/source/land-of-apps/claudecode_gemini_and_codex_swebench/work/claude-appmap-3step/synth__oscar_dup_order_email/20260504_164906/repo/src/oscar/apps/checkout/mixins.py:246-256` — the offending block:
  ```
  246:        # Send the confirmation message early, so the customer receives it
  ...
  250:        try:
  251:            self.send_order_placed_email(order)
  252:        except Exception:
  253:            logger.exception("early confirmation email failed; continuing")
  254:
  255:        # Send confirmation message (normally an email)
  256:        self.send_order_placed_email(order)
  ```
  The "early send" added in lines 246-253 was never paired with the removal of the original send at line 256, so both run on every successful order.
- `/Users/kgilpin/source/land-of-apps/claudecode_gemini_and_codex_swebench/work/claude-appmap-3step/synth__oscar_dup_order_email/20260504_164906/repo/src/oscar/apps/checkout/mixins.py:276-279` — `send_order_placed_email` itself (one-liner that builds an `OrderDispatcher` and dispatches); not buggy, just invoked twice.

## Caveats

- The bug report explicitly rules out signal handlers and Celery retries; the recording corroborates this — only the duplicated direct call within `handle_successful_order` is responsible. There is no second call site outside this method.
- The fix should remove exactly one of the two calls. The wrapping try/except at lines 250-253 was the newer addition (its comment is the rationale); the caller will need to decide whether to keep the early try/except form (and drop line 256) or revert to the original unconditional send at line 256 (and drop lines 246-253). Either preserves single-send behavior; the choice affects only what happens if the email send raises.
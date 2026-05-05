Available balance jumps up at midnight on the day a hold is supposed to expire

What we observed
================

Two consumer-checking customers escalated this week with the same
complaint: their available balance increased at 12:00 a.m. ET on
the day a temporary hold was scheduled to drop, even though the
hold itself was set to release later in the day. One customer was
near their available threshold and ended up authorizing a debit-
card purchase against a balance that the system showed as freed,
but the underlying hold hadn't actually expired yet — it was
scheduled for 5 p.m. ET. The transaction went through and they
ran into an overdraft when the hold released and reconciled
against the new debit.

We pulled audit data for both. The pattern is consistent and
matches a third case Operations had flagged but not opened a
ticket on:

- Hold placed at, say, 6 p.m. yesterday with an expiry timestamp
  of 5 p.m. ET the following day.
- At 11:59 p.m. ET on the placement day, the hold reduces
  available balance correctly.
- Starting at 12:00 a.m. ET (the day of expiry), the available
  balance reads as if the hold has already been released — the
  full ledger balance shows as available.
- The hold's actual release timestamp doesn't fire until 5 p.m.
  ET that same day, but during those 17 hours the customer's
  available balance is wrong.

Pending balance shows the inverse: it goes to zero at 12:00 a.m.
when it should still be reflecting the held amount.

The signal we're getting from customer-care is that this is
specific to "hold expires later TODAY" — holds that run multi-day
look fine while they're more than 24 hours out, and once they're
fully expired (past their release timestamp) the math is right.
The bad window is just the calendar day on which the hold's
expiry timestamp falls, before the timestamp itself has elapsed.

Steps to reproduce
==================

1. In a controlled environment, with the application clock fixed
   to 9 a.m. ET on a weekday (any normal day in 2026 — we used
   2026-04-30):
2. Place a temporary hold of $50 against a consumer checking
   account with a current ledger balance of $200, with an expiry
   timestamp of 5 p.m. ET the same day.
3. Query the account's balance.
4. Expected: available = $150 (ledger 200 minus active hold 50);
   pending = $50.
5. Observed: available = $200; pending = $0.

The same scenario at 11:59 p.m. ET the previous day shows the
expected $150 available / $50 pending. The math goes wrong at
midnight regardless of clock skew, and stays wrong until the
hold's release timestamp.

What we expect
==============

A hold should reduce available balance for as long as its expiry
timestamp is in the future, with timestamp granularity. "Same
calendar day" is not the boundary we publish to customers; the
exact-second expiry is. Until the expiry instant actually
arrives, the hold is active.

What actually happens
=====================

Available balance moves at the date boundary (midnight in the
account's timezone) rather than at the hold's expiry timestamp.
Holds that expire any time later on a given calendar day
disappear from the available-balance calculation as soon as that
day starts.

Impact
======

Two customer-facing incidents already, both with downstream
financial consequences (one authorized purchase, one bounced
auto-debit). The exposure window is up to 24 hours per affected
hold, and any debit authorized in that window can land us in
violation of our hold-honor commitment. We need available balance
to reflect the actual second the hold expires, not the calendar
date it falls on.

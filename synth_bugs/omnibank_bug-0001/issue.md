ACH submissions stamped at 16:45:00 ET are settling same-day when they should roll over

What we observed
================

Treasury Operations flagged a customer credit that hit our ACH
hub with a timestamp of 16:45:00 ET on a Tuesday and ended up
settling same-day instead of rolling to the next business day.
Per NACHA's same-day window, 16:45 ET is the final cutoff for the
afternoon — submissions at or after that time are not eligible
for same-day, submissions strictly before are.

The customer in this case was on a scheduled workflow (their AP
system fires the batch on a cron), so they hit the cutoff dead-on,
not "approximately." We pulled four other cases over the last
calendar week with audit-log timestamps of exactly 16:45:00.000 ET
— all of them landed in the same-day window. Submissions even one
second later (16:45:01) miss the window correctly.

We've ruled out a clock-drift issue: the timestamps come from our
monotonic clock service and read 16:45:00.000 to the millisecond.
The input to the eligibility decision is correct; the decision
itself is wrong at the boundary.

Steps to reproduce
==================

1. In a controlled environment, freeze the application clock to a
   weekday at exactly 16:45:00 ET. (Pick a date that isn't a
   federal holiday and isn't on the weekend — any normal Tuesday
   in 2026 works; we used 2026-04-14.)
2. Ask the system whether a submission at the current time
   qualifies for the same-day window.
3. The system says yes.

What we expect
==============

The eligibility decision at exactly 16:45:00 ET should be **no**
(does not qualify same-day, rolls to next business day). The
boundary is closed on the past side, open on the future side:
strictly before the cutoff qualifies; at or after does not.

NACHA's documented language is "submissions received before 16:45
ET" — "before" in the strict sense. We need to match that exactly
or our risk and operations teams will keep getting paged when a
customer hits the boundary.

Impact
======

We've already had one customer escalation. Their counterparty
received funds same-day when the customer expected next-day,
which messed with their reconciliation. Treasury Ops is doing
manual same-day review on every batch since the regression
landed, which is unsustainable. Risk is also concerned that
similar boundary handling could be wrong in other cutoff checks
we haven't audited yet — any fix should be tight enough that the
pattern is obvious to whoever does that audit.

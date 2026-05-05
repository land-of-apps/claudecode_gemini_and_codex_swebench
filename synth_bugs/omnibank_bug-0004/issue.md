Accrued interest overcharging on positions whose accrual period ends on the last day of February

What we observed
================

A counterparty escalated a small but consistent overcharge on
their corporate term-loan accruals. They had spotted that the
interest billed for a stub period beginning Jan 31 2026 and
ending Feb 28 2026 was higher than the contractual day-count
convention should produce. We traced their trade and reproduced:
for that 28-calendar-day stub, our system is accruing as if the
period were 30 days. The same exact loan, the same rate, the same
notional — but with a stub ending Feb 27 instead of Feb 28 — gets
the right number.

We checked four other portfolios with positions that touch the
end of February. Every position whose accrual end date is exactly
Feb 28 or Feb 29 is computing 2 (sometimes 1) extra days of
interest. Positions whose end date is later in March, or earlier
in February, are unaffected. The smell is "end of February is
being treated as if it were the 30th of the month" — but only
when the 30/360-style convention is in effect. Other conventions
(actual/360, actual/actual) show the correct day count for the
same dates.

The pattern is deterministic. The numbers don't drift; they're
wrong by a clean integer-day delta every time. So this isn't
rounding, isn't FX, isn't day-of-week. It's whatever logic
computes "how many days in the period" under the bond-style
convention and is misclassifying Feb 28/29 as a month-end.

Steps to reproduce
==================

1. In a controlled environment, take any instrument that uses
   the 30/360 (bond-basis) day-count convention.
2. Compute the year fraction for the period:
     start = 2026-01-31, end = 2026-02-28
3. Expected value, by the standard 30/360 formula:
     `(360*(2026-2026) + 30*(2-1) + (28-30)) / 360 = 28/360`
   = 0.077777…
4. Observed value:
     30/360 = 0.083333…
5. Apply that year fraction against any non-zero rate × notional
   and the difference compounds across all positions whose
   accrual end falls on Feb 28 or Feb 29.

What we expect
==============

For 30/360 on Jan 31 → Feb 28: the day count should be 28, the
year fraction 28/360 ≈ 0.07778. February's last day is NOT
treated as the 30th by this convention. The 30/360 rule (per
ISDA 2006 §4.16) only normalizes the END day to 30 when the
start day was already 30 or 31 AND the end day is 31 — never
based on calendar end-of-month.

What actually happens
=====================

Year fraction comes back as 30/360 ≈ 0.08333 instead — Feb 28 is
silently snapped to "day 30" before the formula runs. Roughly +2
days of accrued interest on every accrual-period boundary that
lands on Feb 28; +1 day on Feb 29 (which would happen on leap
years).

Impact
======

We estimate a low-five-digit dollar over-accrual across our book
each February, plus the counterparty escalation that started
this. The bigger concern is regulatory: 30/360 is an industry-
standard convention with a published spec, and shipping the wrong
formula on instruments that reference it puts us out of step with
how counterparties model the same trades on their side. Reconcile
mismatches will keep showing up at every coupon date and
maturity that lands on Feb 28/29 until this is fixed.

Whatever was added to handle Feb end-of-month special-casing
needs to come out — the 30/360 convention is documented as not
needing it. The fix should leave the 30/31 rule intact (that
one's correct) and stop touching February.

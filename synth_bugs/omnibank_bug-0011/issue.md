Fee and interest calculations rounding to two decimals before the multiply — losing real money

What we observed
================

Three product teams in the past week have complained about
small but systematic discrepancies between expected and computed
amounts whenever a percentage is applied to a money value. The
common thread: anywhere we have a basis-points percentage (e.g.
375 bps, 1825 bps) and apply it to a dollar amount, the result
is off by a tiny amount in a way that scales with the size of
the principal but is independent of the currency or rounding
convention at the *output* end.

Concrete examples we walked through with finance:

- A loan with a 18.25% APR (1825 bps) accruing on a daily
  notional of $1,000,000 should accrue ~$500.00 of interest
  per day (1825/10000 × 1,000,000 / 365). Pre-regression we got
  $500.00 to the cent. Post-regression we get $500.00 some
  days, $0.00 some days, and miscellaneous values like
  $20,000.00 on others — clearly the percentage is being
  pre-rounded to 2 decimal places before the multiply, so
  18.25% becomes 0.18 (close-ish), but a fee of 0.5% (50 bps)
  becomes 0.01 (round to 2dp from 0.005, half-even rounds to
  even = 0), then 0% × any amount = $0.

- A 0.25% (25 bps) fee on $50,000 should be $125.00. We get
  $0.00.

- A 12.345% rate (1234.5 bps, fractional bps) used by one of
  our partner programs gives 0.12 on the multiply (rounded to
  2dp from 0.12345), so $100,000 × that gives $12,000 instead
  of $12,345.

Pattern: the percent value is being rounded to **2 decimal
places** at the moment we compute the fraction, then multiplied.
Pre-regression we used much higher internal precision (10
decimal places I think someone said) and only let the result
round at the Money boundary, which is what you want — Money
already enforces cent precision at construction.

Steps to reproduce
==================

1. In a controlled environment, construct a percentage of 25
   basis points (0.25%, or `Percent.ofBps(25)`).
2. Apply it to $50,000 (`Money.of(50000, USD)`).
3. Expected: $125.00.
4. Observed: $0.00.

Or, less extreme: apply 1825 bps (18.25%) to any non-trivial
amount and compare to the long-form `(bps × amount) / 10000`
calculation. The mid-step rounding produces results that
disagree by 1-3 percent in either direction depending on which
side of the 0.005 boundary the percent lands on.

What we expect
==============

When converting basis points to a fraction (multiplier between 0
and 1) for use as a multiplier on a Money value, the
intermediate fraction must carry enough precision that the
information from the basis-points input survives. The Money
class will round the FINAL product to the cent (its
constructor/factory does that already, half-even); that's
where the rounding belongs. Mid-computation precision should be
high (at least the maximum precision of any basis-points
representation we accept, which is fractional bps in some
configs).

What actually happens
=====================

The fraction is rounded to 2 decimal places before the multiply.
Anything below 0.005 disappears (becomes 0). Anything from
0.005-0.014 becomes 0.01. The signal from sub-percent precision
is destroyed before it reaches the multiply. The Money output
still LOOKS reasonable in isolation (it's a valid dollar value,
to the cent) but the underlying math is wrong.

Impact
======

Across our loan-servicing book, accrual numbers run end-of-day,
so the wrong-on-some-positions effect compounds. Three customer
escalations so far on individual loans where the visible
disagreement was large enough to notice; finance suspects more
positions are quietly off by sub-dollar amounts but visible at
quarter-end aggregation. Fees are similarly affected: any fee
configured at fractional-percent precision (which is most of
them — 0.25%, 0.50%, 1.5%) is currently mis-computed.

The fix is to restore the higher mid-computation precision on
the basis-points-to-fraction conversion. The Money side of the
math is already doing the right thing at construction.

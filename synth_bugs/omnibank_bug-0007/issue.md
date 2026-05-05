Final installment on amortized loans no longer zeros the principal — pennies stuck after payoff

What we observed
================

Servicing flagged ten corporate term-loan accounts where the loan
is "scheduled to be paid off" by the maturity date but the
closing principal balance after the final scheduled payment
isn't zero — it's a few pennies, sometimes a few dollars, off.
We pulled one example: $5M facility, 60 monthly installments,
fixed rate; on a perfect amortization that loan should hit zero
on installment 60. After installment 60 the schedule shows a
remaining balance of $0.43.

The borrowers are paying their full scheduled installment on
each due date, on time. The math is right per-installment within
the precision we use elsewhere — no missed payments, no off-cycle
draws. But on the final installment specifically, a tiny residual
that should have been absorbed isn't.

We compared schedule output for two loans of identical structure
(same notional, term, rate, day-count). The pre-regression export
zeroes balance to the cent on the final installment; the post-
regression export carries a residual of a few cents to a few
dollars depending on the size and term of the loan. Mid-loan
installments look identical in both versions — only the LAST
installment differs.

Steps to reproduce
==================

1. In a controlled environment, build a fixed-rate, fully-
   amortizing loan: any non-trivial principal (e.g. $1M), any
   non-zero rate, any term length where the standard amortization
   formula won't divide cleanly into round cents (most of them).
2. Generate the full repayment schedule.
3. Sum the principal portions across all installments.
4. Inspect the final installment's closing balance.

Expected: closing balance after the final installment is exactly
zero. The cumulative principal across the schedule equals the
original loan amount to the cent.

Observed: closing balance after the final installment is some
small non-zero amount (positive, in our cases — meaning a residual
the borrower hasn't paid down). That residual reflects the
accumulated penny-rounding across the prior installments and
should have been absorbed onto the last payment.

What we expect
==============

Industry-standard amortization rounds each installment's
principal split to the cent and lets the residual roll. The final
installment is then computed as "whatever balance is left," not
the formula's level-payment value — so the principal portion of
the final installment includes the cumulative rounding residual
and the closing balance lands on exactly zero. We need that
behavior back.

What actually happens
=====================

The final installment is computed identically to a mid-loan
installment: principal-portion = scheduled payment minus
interest-on-current-balance, plus the level payment. That
ignores the residual. The schedule shows installments 1..N-1
correctly, then installment N looks normal but doesn't absorb the
remainder, and the closing balance sits at the residual forever.

Impact
======

Ten loans flagged so far, residuals between $0.01 and $4.18.
Operationally these are nuisance items — servicing has to
manually post a write-off journal to clear them out. Customer-
facing, payoff statements show non-zero balances after the final
scheduled payment which is confusing borrowers ("we paid the full
schedule, why do we still owe forty cents?"). At quarterly close,
ten unintentional residual balances become reconciliation noise.

The fix should restore the convention: the FINAL installment
absorbs whatever principal is still on the books at that point,
regardless of what the level-payment formula would produce on its
own. Mid-loan installments should remain unchanged.

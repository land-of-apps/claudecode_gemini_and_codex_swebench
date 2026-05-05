Active corporate loans appearing on the books with no funding event recorded

What we observed
================

Treasury reconciliation flagged three corporate loans this week
where the loan record is in the live/active state but there's no
corresponding disbursement entry in the ledger. Normally we'd see
a disbursement journal posted when the loan is funded — that's
the entry that moves cash from our funding account onto the
borrower's. For these three, the cash never moved on our side,
yet the loan is being treated as live (accruing interest, showing
up in performance reports, due-diligence ready).

The borrowers haven't drawn yet either, which is part of why this
came to light: a corporate-banking RM was confirming the funding
date with one of them and was told "no, we never received the
funds." Pulling the audit trail for that loan, the status moved
from "approved" straight to "live" with no funding step in
between. We assumed the disbursement step was missed manually —
but two more loans showed the same pattern under different RMs,
so this is systemic.

Loans that DID get funded look correct: a funding event sits
between approval and going-live, with the disbursement journal
attached, the timing right, and downstream events firing as
expected. The bad set are unanimously skipping that step.

Steps to reproduce
==================

1. In a controlled environment, take a loan that's been approved
   (status = approved) but not yet funded.
2. Drive the workflow that flips a loan to its live/active state.
3. Observe whether that workflow accepts the transition straight
   from approved without requiring funded as an intermediate.

We expected the system to refuse: a loan should only become live
AFTER it's been funded, never before. Funding is a hard
prerequisite for a loan being able to accrue interest, take
payments, etc. — without it the loan shouldn't be visible in any
operational queue as if it's a real, drawing facility.

What we expect
==============

Approved → live should be a *blocked* transition. The only path
into the live state should be from funded. If a caller asks the
system to flip a loan straight from approved to live, the system
should refuse and surface the funding requirement.

What actually happens
=====================

The system accepts approved → live as a legal transition. There's
no error, no warning, no audit-log line. The funding gate is
gone.

Impact
======

Three loans currently in this state, totaling roughly $4M
notional. None have drawn but all three are showing in our
performance dashboards as if they're funded. The bigger risk is
audit and regulatory: corporate loans flowing through the lending
workflow without a funding entry creates a gap between the
lending system's view of the borrower and the ledger's, which
will be flagged at the next quarterly review.

The fix is straightforward in spirit: tighten the legal-transition
rule so funded is a required step. We need this back to the way
it used to behave — and verified for any other places downstream
that may have inferred funding from the live state instead of
checking explicitly.

Three corporate loans showing as live without any money moving

Treasury reconciliation flagged three large corporate loans this
week that look funded on our side but no cash has actually moved
out of our funding account. They're showing in performance
dashboards, accruing interest, and listed as drawing facilities,
but the borrower hasn't received anything and we have no
disbursement entry in the ledger for any of them.

How we found it
===============

A relationship manager called one of the borrowers to confirm
their funding date. The borrower said "we never received the
funds." The RM checked our system — the loan was marked as live.
She escalated. Two more loans turned up over the next two days
under different RMs with the exact same shape: live on our side,
no cash on the borrower's side, no disbursement journal posted.

Total exposure on the books: ~$4M notional. Nobody has drawn yet,
so no real money is at risk today, but our reports are wrong and
the next quarterly audit is in five weeks.

Loans that DID get funded look right
====================================

We pulled audit trails for a dozen recent loans that were funded
normally. Every one of them shows the funding event recorded
between approval and going-live, the disbursement entry posted
to the ledger at the same time, and the rest of the downstream
flow firing in order.

The three problem loans don't have that funding event in their
audit trail at all. They look like they jumped from the approved
state directly to the live state, with no funding step in
between.

We've ruled out
===============

- Manual override / RM error. We can't find any path through the
  UI or the RM-facing tools that lets someone bypass the funding
  step. RMs we've asked say they did the normal flow.
- Data fix-ups by ops. Audit log doesn't show any manual edits
  to the loan records.
- Migration / batch job. The three loans were created at three
  separate times by three different RMs, all within the past
  week. No common batch.

What we expect
==============

A loan should not be possible to make live until it's been
funded. The funding step is a hard prerequisite — without cash
moving on our side, the borrower can't draw and our books are
fiction. The system should refuse the operation if anyone tries
to make a loan live without a funding event recorded.

What we want
============

Find the path that's letting these three loans become live without
funding. Close it. Make sure the operation is rejected — with a
visible error, not silently — anywhere in the codebase that does
this kind of state change.

The bigger concern is downstream code that may have ASSUMED a
loan being live implied funded. If any reporting, accrual,
statement-generation, or compliance code reads "live" as a proxy
for "funded", those readings are now wrong for these three loans.
Worth scanning.

Impact
======

Three loans currently mis-stated, ~$4M notional. Audit risk is
the bigger exposure than the customer impact (no one's been
charged or denied service). Quarterly review is in five weeks
and this is the kind of thing it'll flag.

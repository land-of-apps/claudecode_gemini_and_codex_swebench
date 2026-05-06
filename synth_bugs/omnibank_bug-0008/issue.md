Customers being charged twice on retried payments

Six customers this week have written in saying they got billed
twice for the same payment. Looking at our records, they're
right — there are two separate transactions in our system with
the same memo, the same amount, the same beneficiary, posted
within a second or two of each other. The customers got two
confirmation emails too.

Our retry contract is supposed to prevent exactly this. When a
customer's app times out or they hit "send" twice, the second
attempt is supposed to come back with the same transaction we
already created — not generate a new one. That's the whole
reason our payment app sends a unique reference with every
submission: so we can recognize "oh, we've seen this one, here's
your existing transaction" instead of creating a duplicate.

Three things we've seen happen
==============================

It comes in three flavors:

1. Customer's statement shows two debits, both posted, both for
   the same payment. (Worst case — we have to manually reverse
   one and apologize.)
2. The customer's app got back a "something went wrong" error on
   the retry, but their statement shows the original payment
   went through fine. Confusing for the customer because they
   don't know if it actually worked.
3. The customer's app got back two different transaction IDs for
   what was supposed to be the same payment. Reconciliation
   nightmare downstream because we can't tell which one is the
   "real" one to keep.

Pattern
=======

Every one of these incidents was a customer hitting "send" twice
in quick succession (or their app retrying automatically because
the first response didn't come back fast enough). Talked to one
of the affected customers — she said the spinner kept going, so
she tapped the button again. Both her taps got through.

We don't see this when customers wait and retry slowly. A retry
30 seconds later always behaves correctly. It only happens when
the two attempts arrive within milliseconds of each other —
basically simultaneously.

What we expect
==============

If two payment attempts arrive at the same time with the same
unique reference, we should process exactly ONE of them and
return the same transaction ID for both. That's the contract
our app and our public API both publish. Whatever was protecting
us from duplicates before broke at some point — we need it back.

Caller-side retry caps don't fix this for us. The customer's app
doesn't know it's racing — it's just retrying because nothing
came back fast enough. The protection has to live on our side.

Impact
======

Six tickets this week, four of them from corporate customers
(who run automated retries from their treasury systems and so
hit this pattern much more often than human users tapping a
button). Reconciliation team has been manually pairing up the
duplicate transactions and flagging them for reversal. We've
had to credit two customers for double-charges that landed in
their account before we caught them.

Higher-volume corporate accounts are the bigger risk. Their
treasury systems are the most likely to retry quickly, and
they're the customers with the lowest tolerance for "we billed
you twice, sorry."

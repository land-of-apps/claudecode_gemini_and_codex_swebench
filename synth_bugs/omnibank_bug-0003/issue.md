Mixed-currency journal lines landing in the ledger and breaking per-currency reconciliation

What we observed
================

Reconciliation flagged five journal entries posted between 04-22 and
04-29 where, on a single entry, one or more posting lines have an
amount in one currency but reference a GL account that's denominated
in a different currency. The journals balance in aggregate dollar
terms — debits sum to credits — but when we slice the postings by
currency the per-currency totals don't balance. Reconciliation
treats those journals as inconsistent and refuses to net them
against the upstream feeds, so they sit in a manual-review queue.

The pattern looks the same on each one: a journal carries two or
more lines, all balanced by amount, but at least one line's amount
currency doesn't match the currency of the GL account it posts to
(USD line landing on an EUR liability account, GBP line landing on
a USD asset account, and so on). These journals should never have
been accepted — the line amount and the account it touches must be
in the same currency, otherwise the FX of the position is undefined.

We verified the upstream sources: the originating systems are
emitting balanced lines. The balance check on the journal as a
whole is doing its job. Whatever was supposed to prevent a single
posting line from referencing an account in a different currency
than its own amount is not running, or is running and not raising.
The persistence layer is happily storing the entry as-is.

Steps to reproduce
==================

1. In a controlled environment, construct a journal entry whose
   debit and credit lines balance by amount (e.g. two lines of
   $100 each, one debit, one credit), but where one of the
   referenced GL accounts is in a different currency than the
   line amount (e.g. the debit hits a USD asset account, the
   credit hits an EUR liability account).
2. Submit the journal through the posting path that consumer
   account opening, ACH credits, and wire transfers all use.
3. Inspect the ledger.

What we expect
==============

Submission should be rejected before persistence with a clear
indication that the line currency doesn't match its account's
currency. The journal must not appear in any downstream query —
not in account balances, not in journal lookups, not in event
streams — because once it lands the FX position becomes
ambiguous and reconciliation can't unwind it without manual
intervention.

What actually happens
=====================

The journal is persisted. Account balances and journal lookups
return it. Downstream events fire. Reconciliation has to flag it
hours later, and the only way to clear it is to manually back out
the entry and re-post the corrected version.

Impact
======

Five journals in the queue right now, all from automated upstream
feeds. Two are wire-related (large dollar values) and three are
ACH credits where the originator has multi-currency batches. The
manual review per entry is roughly 20 minutes of an analyst's time.
More worryingly, until this gate is restored, any new automated
feed that emits a wrongly-currency-coded line will quietly land
in the ledger — finance has no way of knowing how many we're
missing without scanning every recent journal line by line.

The check was specifically designed to catch this BEFORE persist;
that's where we need it back. A post-persist scrubber is not
acceptable — by then we've already broadcast events, updated
balances, and begun downstream propagation.

Audit portal painfully slow for big corporate accounts since the release

Operations engineering paged us yesterday afternoon. The audit
portal — the internal tool reconciliation analysts use to pull a
corporate account's full transaction history — is taking 5 to 15
seconds to load any account with more than ~200 entries in the
requested date range. Smaller accounts still come up fast.
Larger accounts either time out or are unusable.

We compared response times against the prior release using the
same accounts and date ranges:

| Account size  | Prior release | Current release |
|---------------|---------------|-----------------|
| ~50 entries   | ~30ms         | ~250ms          |
| ~200 entries  | ~80ms         | ~1.5s           |
| ~500 entries  | ~200ms        | ~5-6s           |
| ~1000 entries | ~400ms        | ~12-15s (timeout) |

The growth is roughly linear in the number of entries — twice as
many entries means roughly twice as long. That's the shape that
made us suspect this is something doing per-entry work that
shouldn't be.

The same internal API also feeds reconciliation worksheets, which
is what the analysts noticed first. The largest dozen corporate
accounts went from sub-second to 6-15 seconds. Above a few
seconds the analysts stop waiting and Slack-ping us instead of
clicking refresh.

Customer-facing endpoints
=========================

Customer-facing statement endpoints aren't affected — they go
through a different code path and feel as fast as before.
Anything internal that walks the journal history is affected.

Database is fine
================

We checked first. Database CPU is normal, no missing indexes, no
slow-query log entries — each individual query against the data
is sub-millisecond. The slowness is in the application layer
issuing too many queries, not in the database executing any one
of them.

The shape of "how many queries get issued" looks like it scales
with the number of entries. For an account with 318 entries in
the date range, we counted hundreds of queries against the lines
table during a single audit-portal page load.

What we expect
==============

Loading an account's history for a date range should issue a
small, bounded number of database queries — independent of how
many entries are in the result. Two or three SQL statements per
page load, regardless of whether the page is showing 10 entries
or 1000.

The previous release behaved this way. Something in the most
recent release broke it.

What we want
============

Find what changed in the journal-history retrieval that caused
queries to scale per-entry. Restore the bounded behavior.
Confirm with a re-run on the same staging snapshot that we're
back to a small constant number of queries.

Impact
======

Audit portal team is asking us to roll back the release if we
can't get this fixed today. Reconciliation analysts can't do
their morning workflow on the largest accounts. No
customer-visible impact, but blocking internal users.

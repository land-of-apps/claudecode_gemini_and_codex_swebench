Account journal-history fetch is making one query per row, slowing reconciliation by 50x

What we observed
================

Operations engineering paged us yesterday afternoon: the
account journal-history endpoint, which we use to populate
reconciliation worksheets and which the audit-portal calls when
loading a corporate account's transaction history, started
timing out for any account with more than ~200 journal entries
in the requested period. Smaller accounts work; larger accounts
either time out or the page is unusably slow.

We enabled SQL logging on staging and reproduced. For an account
with 318 journal entries in the date range, the endpoint issued
one parent query that returned the journals, then one
child-query per journal to fetch that journal's posting lines —
318 follow-up queries, executed serially. Each is fast on its
own (sub-millisecond), but the round-trip count adds up: a
month-of-history fetch that used to take ~80ms now takes 4-6
seconds. We compared a recent staging snapshot against the
previous release: same data, same account, same date range —
the previous release issues 1-2 queries total, current release
issues 1 + N.

This is the "N+1 query" pattern. The journal record loads, then
its line collection loads lazily on first access, and our
serializer touches the lines for every journal. So reading the
list of journals trips one query for the journals + one
per-journal query for that journal's lines.

This isn't a database problem (indexes are fine, the engine is
fast). It's the JPQL we're issuing. The query that returns the
journals isn't fetching the line collection along with the
parent rows in the same statement. Pre-regression it did; post-
regression it doesn't.

Steps to reproduce
==================

1. In a controlled environment, seed an account with ~50
   journal entries, each with 2-4 posting lines.
2. Call the query that returns journals for that account over
   a date range covering all of them.
3. Iterate the result list and access `lines` on each returned
   journal (which is what our serialization layer does).
4. Watch SQL.

Expected: 1 query. Maybe 2 if the engine splits the join across
two roundtrips for some reason — but bounded by the structure
of the query, NOT by the number of journals.

Observed: 1 + N queries (one per journal in the result set).

What we expect
==============

The journal-history query must materialize each journal's
posting lines in the same SQL roundtrip as the parent journal
rows. JPA expresses this as a fetch join — i.e. instead of
plain `JOIN j.lines l`, write `JOIN FETCH j.lines l`. Without
the fetch keyword, the join filters but doesn't eagerly populate
the collection on the parent side, so Hibernate falls back to
its lazy loader for each parent on access.

Whatever was generating the JOIN clause for that endpoint's
query lost the FETCH keyword in the last release. We need it
back.

What actually happens
=====================

The query plan has a JOIN that filters by the right account and
date range and returns the right journal rows, but the parent
rows arrive without their line collections initialized. On
serialization (or any access to the lines property), Hibernate
issues a one-off SELECT against journal_line for each parent.
N parents → N child queries.

Impact
======

Performance regression severe enough that the audit-portal team
is asking us to roll back the release. Reconciliation worksheets
that pull journal history for the largest dozen corporate
accounts went from sub-second to 6-15 seconds, which is the
practical threshold above which the analyst Slack-pings
engineering instead of clicking refresh. Customer-facing
statement APIs aren't affected (different code path) but any
internal tool that walks journal history is.

The fix is in the JPQL of the journal-history query: the join to
the lines collection needs to be a *fetch* join. Restore the
FETCH keyword that the last release dropped.

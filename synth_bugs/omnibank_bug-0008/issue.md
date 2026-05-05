Duplicate payment rows on retried submissions — same idempotency key landing twice

What we observed
================

Operations flagged six payment-submission incidents in the past
ten days where a single client request produced two different
payment IDs in our system, both tied to the same idempotency
key. The pattern is consistent: the client retries an in-flight
submit (network hiccup, app refresh, double-click on the "send"
button) and instead of getting back the same payment ID, gets
back a different one. By the time we look at the database,
either there are two rows with the same idempotency key (when
the second insert managed to land before our unique-index
caught up), or the second submit returns an unhandled error
back to the client even though the first submission landed
fine.

Idempotent retry is the contract we publish for our payment
endpoints — submitting the same request twice with the same key
is supposed to return the same PaymentId both times, never
create a second payment, never error. We've been seeing all
three failure modes:

- Two PaymentIds returned for the same key (rare, only when the
  unique index races slowly enough to allow both inserts).
- One PaymentId returned, then a 500-class error on the retry
  with a unique-constraint message in our logs (more common).
- One PaymentId returned, then a different PaymentId returned
  (worst case for reconciliation — caller can't tell which one
  is the "real" payment).

These all stem from concurrent calls. None of the affected
incidents involve sequentially-spaced retries (e.g. a client
waiting 30 seconds and retrying); every one was two requests
landing within milliseconds of each other.

Steps to reproduce
==================

1. In a controlled environment, mock the payment repository so
   it enforces a unique constraint on `idempotency_key`
   (i.e. a second insert with the same key throws).
2. Construct a single PaymentRequest with a fixed idempotency
   key and otherwise valid fields.
3. From two threads, simultaneously call submit() on the same
   service instance with the same request. Use a CountDownLatch
   or similar to release both threads at once.
4. Assert: both calls return the same PaymentId, and the
   repository contains exactly one row for that key.

Expected: the two submits serialize. Whichever thread is "first"
inserts the row; the second thread sees the row already exists
on its lookup and returns the same PaymentId. No exceptions.
One row total.

Observed: both threads' lookups race past "no row exists"; both
proceed to allocate a new PaymentId and call save. The second
save fails the unique-key constraint OR (in the worst case)
both saves succeed and we have two rows.

What we expect
==============

Concurrent submit() calls with the same idempotency key MUST
return the same PaymentId and produce exactly one persisted
row. Whatever serialization existed in the original
implementation needs to be back. Caller-side serialization
isn't a substitute — the contract is that the SERVER protects
against duplicates, regardless of how the client retries.

What actually happens
=====================

The lookup-then-save sequence has no mutual exclusion across
threads. Two concurrent calls both observe "no row" at the
findByIdempotencyKey step and both attempt to create a new
payment, leading to one of the failure modes above.

Impact
======

Six incidents in ten days, four of which surfaced via customer-
care tickets ("did my payment go through? I got two
confirmation emails / two SMS / two debits showing pending").
The exposure scales with submission volume — high-volume
corporate clients running automated retries are most at risk.
Reconciliation has to manually pair up the duplicate IDs
against the bank-side instructions to avoid double-pay events
downstream.

The fix is the per-key serialization that used to be there.
Caller-level retry semantics shouldn't differ from sequential
retries — same key in, same payment ID out.

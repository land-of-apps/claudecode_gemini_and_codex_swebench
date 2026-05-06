Hold release fires at expiry timestamp instead of after — single-nanosecond gap where balance is wrong

What we observed
================

A test-engineering pass on our hold-lifecycle code surfaced an
off-by-one boundary issue. We've been documenting the contract
for temporary holds as "the hold is active for the period
beginning when it's placed, up to AND INCLUDING the expiry
timestamp; once that timestamp has passed (i.e. strictly
greater), the hold is inactive." That's the convention finance
and risk negotiated with the product team last year — it lines
up with how settlement-finality timestamps work elsewhere in
the bank.

When we wrote a unit-precision test against the live behavior,
asking "is this hold active at exactly its expiry timestamp?",
the answer came back **no**. Released. Same hold, asked one
nanosecond before expiry: yes, active. Same hold, asked one
nanosecond after: yes, inactive. The boundary itself is being
treated as exclusive — the hold flips to "released" the moment
the clock equals the expiry timestamp, not the moment it has
passed.

This is small in wall-clock terms but real in semantic terms:
our customer-facing language says "your hold expires at 5:00
p.m." and at exactly 5:00:00.000000000 p.m. the hold is
supposed to still be visible (it's expiring AT that instant,
not AT the previous instant). Our audit log timestamps and
release-event firing should align with the timestamp they
publish.

We checked: the previous release of this code path treated the
expiry as INCLUSIVE — at the expiry instant the hold was still
active, and at the next observable instant it was released.
That matched the published contract. After the most recent
change in this area, the expiry instant itself is being treated
as already-released.

Steps to reproduce
==================

1. In a controlled environment, place a temporary hold with
   expiry 2026-04-30T13:00:00Z (one hour in the future from a
   fixed "placed" time of 2026-04-30T12:00:00Z).
2. Ask whether the hold is active at exactly 2026-04-30T13:00:00Z.
   Expected: true. Observed: false.
3. Ask whether the hold is active at 2026-04-30T13:00:00Z plus
   one nanosecond. Expected: false. Observed: false.
4. Ask whether the hold is active at 2026-04-30T13:00:00Z minus
   one nanosecond. Expected: true. Observed: true.

Step 2 is the failing case. The boundary at the exact expiry
moment should be inclusive — the hold "expires AT" that moment
but is still active there.

What we expect
==============

The active-window decision should treat the expiry timestamp as
INCLUSIVE — a hold whose expiry timestamp equals the
observation timestamp is still active at that instant. A hold
becomes inactive only after the observation timestamp has
strictly passed the expiry. This matches the published
"hold active until expiry" customer-facing language.

What actually happens
=====================

The active-window decision treats the expiry timestamp as
EXCLUSIVE. At the moment the observation timestamp equals the
expiry timestamp, the hold is reported as inactive. The single-
instant gap is small but the contract says "until expiry,"
which means up to and including that instant.

Impact
======

Subtle but real for any code that snapshots state exactly at
a boundary timestamp (some of our reconciliation jobs do, and
audit-log generation does). It also makes our published API
documentation wrong — anything that reads "hold active until
expiry" is technically false today. The hold lifecycle's
release path (manual release; "released at" timestamp set) is
unaffected; only the time-based active-window check is wrong.

Customers are receiving duplicate order confirmation emails

What we observed
================

Several customers have written in over the past week reporting that
they got TWO order confirmation emails for the same order — same
subject, same content, same order number — about a second apart.
Our support inbox is filling up with confused replies ("did my order
go through twice?", "do I owe twice now?") and we have to manually
reassure each one.

We checked our email logs in the dispatcher and yes, two distinct
SEND events go out per checkout. The order itself only exists once
in the database, so this isn't a duplicate order — just a duplicate
email per real order.

Steps to reproduce
==================

1. As a logged-in customer, complete a normal checkout flow with any
   product. Use a real email address you can check.
2. Watch the inbox.

Two emails arrive within a couple of seconds of each other. Both
reference the same order number. Both have the standard "thanks for
your order" template content.

Things we ruled out
===================

- Not a re-submitted form (we use post-redirect-get; the success
  page is a GET).
- Not a duplicate signal handler — we have one signal handler
  attached to `order_placed` for analytics, but our confirmation
  email is *not* signal-driven (it goes through the standard order
  email pathway).
- Not a Celery retry (we don't retry order emails on failure).
- Not the email server's fault — the message-id headers differ, so
  these are two distinct sends, not a single send delivered twice.

What we expect
==============

One confirmation email per successful checkout. The fix should
preserve the existing behavior for everything else — only the
duplicate send should go away.

We don't have a smaller repro than the staging shop. Anyone driving
the checkout flow should be able to observe two emails landing in
the LocMem outbox per `place_order` round-trip.

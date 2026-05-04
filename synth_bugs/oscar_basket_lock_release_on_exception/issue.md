After a payment failure, customers can't modify their basket

What we observed
================

We're getting a stream of support requests from customers saying
their basket is "broken". They can see all their items on the
basket page, but the +/- quantity buttons silently do nothing,
the "remove" link silently does nothing, and adding new products
from elsewhere on the site appears to succeed but the new product
never shows up in the basket.

The pattern: every customer who reports this had a payment attempt
fail just before. Not a card-decline (those are recoverable, our
gateway returns a friendly "wrong card number" message and the
customer retries). The pattern is server-side payment errors —
gateway timeouts, gateway-down events, the kind of thing where we
send an admin email and apologise to the customer.

After hitting one of those errors, the customer is shown the
preview page with a generic "we couldn't process payment, please
try again" message. They click back to their basket… and the
basket is silently un-editable.

Working around it
=================

Right now we're telling these customers to clear their cookies and
start a new session. That works but it's a terrible experience —
they lose any selections they had, and they assume our site is
broken (which, fair).

Steps to reproduce
==================

1. As a logged-in customer, build a basket of any size.
2. Proceed to checkout, hit the payment-details page, submit.
3. Use a card that triggers a server-side payment error from the
   gateway (we have one for sandbox testing). The "we couldn't
   process payment" page renders.
4. Click back to the basket from the menu.
5. Try to remove an item, change a quantity, or add a new product.
   Nothing happens.

What we expect
==============

After a payment failure, the customer should be able to keep
editing their basket — change items, remove things, retry checkout
with a different card, etc. The "anticipated" payment errors
(card declined etc.) DO let them keep editing — that path works.
It's specifically the unanticipated / server-side payment error
path that leaves the basket unusable.

We don't know what's specifically different between the two error
paths. They both render error messages, both log, both let the
customer return to checkout. Something the working path does
that the failing path doesn't is the missing piece. Please find
it and put it back.

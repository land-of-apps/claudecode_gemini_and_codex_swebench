Exclusive vouchers stack when they shouldn't

What we observed
================

A customer reported that they were able to apply two of our promotional
vouchers to the same basket and receive both discounts at checkout —
even though the promotion config has both vouchers marked exclusive,
which is meant to mean "only one of these can apply per order."

We can reproduce in our staging shop:

  1. Set up two distinct vouchers, each linked to its own benefit.
     Both promotions are configured the same way: same priority,
     same exclusivity flag, different codes.
  2. Add the first voucher code to a basket — it applies, basket total
     drops as expected.
  3. Add the second voucher code to the same basket — it ALSO applies.
     Both discounts are now showing.

What's strange: the order matters. If you add them in the other
sequence, the second one gets refused with the usual "this voucher
cannot be combined" message. So the rule is being checked, just not
symmetrically.

What we expect
==============

The exclusivity check should be order-independent. If two vouchers
both opt into "I am exclusive," only one of them can be live on a
basket at a time, regardless of which the customer enters first.

We don't have a clean smaller repro than the staging shop yet — it
involves the basket pipeline, the voucher form, and the offer
applicator. If you spin up a basket and apply a couple of exclusive
offers in the order [B, A] vs [A, B] you'll see one of those orders
behaves differently from the other.

Please find the cause and fix it. We don't want a workaround in our
basket form — the rule belongs in core. Make sure the fix doesn't
weaken the existing exclusivity behavior for the single-exclusive-offer
case (which is currently working).

Discounts are running 1-2p higher than expected — reconciliation drifting

What we observed
================

Finance flagged a small but persistent reconciliation drift at
month-end. On orders where a percentage discount was applied,
the discount amount on the receipt and order detail page is
*sometimes* 1p higher than what we'd expect from a quick manual
calculation. It's small per order, but across the month it adds
up and we can't close the books cleanly.

The drift only shows up on percentage discounts. Absolute-value
discounts ("£5 off") match exactly, every time. Free-shipping
discounts also fine. So the problem is somewhere in the
percentage benefit math, specifically when the discount math
hits a half-cent boundary.

Steps to reproduce
==================

1. In a Python shell against the same Oscar version:
       >>> from decimal import Decimal as D
       >>> from oscar.apps.offer.models import Benefit
       >>> Benefit().round(D('0.025'))
2. Observe the result. We expect this to be `Decimal('0.02')` —
   half-cent fractions should round down (this is the merchant-
   favouring convention this shop has used since launch). Instead
   we're seeing `Decimal('0.03')`.

You can also see this in the basket: a £0.05 line with a 50%
percentage offer should produce a £0.02 discount. Currently it
produces a £0.03 discount, which is the customer-favouring side
of the rounding choice. Multiplied across many lines and many
orders, this is the source of the reconciliation drift.

What we expect
==============

Half-cent boundaries should round DOWN, in the merchant's favour.
This is how Oscar has worked historically and matches the
expectation in our finance reconciliation tooling.

We don't think any of the percentage-discount math itself is
wrong — the issue is the rounding convention specifically. The
fix should be small. Please find what changed and put it back.

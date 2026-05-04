Exclusive voucher offers are not exclusive

Issue Summary
=============

When you create two new offers with the voucher offer type and make
them exclusive, it's still possible to add both to your basket.

Though reproducing was a bit strange, as it depends on the order
you're adding the voucher.

I had to add my latest created voucher first and then the first one
I created. The other way around resulted in a message that it wasn't
possible to add that voucher to my basket.

Steps to Reproduce
==================

1. Create two offers, both exclusive and with the "voucher" type.
2. Create two vouchers, link one to the first offer you created and
   one to the second one.
3. Try adding them both to your basket, try it in two ways:
   1. Add voucher one and then voucher two
   2. Add voucher two and then voucher one
4. You'll see the voucher can be applied twice, even though it's
   exclusive.

Technical details
=================

I have tested this against the latest sandbox.

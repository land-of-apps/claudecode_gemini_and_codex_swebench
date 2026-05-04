Voucher codes from our email campaigns aren't being accepted

What we observed
================

Marketing has been running a "spring24" promo for a couple of weeks.
The voucher code goes out in the customer newsletter in lowercase,
because that's what the design team typed into the email template.
Customers are pasting the code from the email into the basket form
and getting "No voucher found with code 'spring24'." If they
manually retype the code in upper case ("SPRING24"), it's accepted
and the discount applies as expected.

We've checked the voucher in the dashboard — it exists, it's
active, the dates are right. The dashboard shows the code as
"SPRING24" (capitals), but the customer-facing form should be
case-insensitive: that's how it's worked for years, and other
shops we operate behave the same way.

Steps to reproduce
==================

1. As staff, create a voucher with the code "SPRING24" via the
   dashboard. Set start/end dates around today.
2. As a customer, add a product to the basket.
3. Submit the basket-voucher form with the code field set to
   "spring24" (all lowercase).
4. Observe the error message: "No voucher found with code 'spring24'."
5. Re-submit with "SPRING24" — works.

What we expect
==============

Submitted codes should resolve to the voucher regardless of case.
Operationally we can't easily ask marketing to capitalise codes
when they paste them into email templates — the codes look ugly in
copy and we use lowercase for branding. The redemption flow needs
to handle both cases.

We don't think this is data: the voucher rows in the DB look
correct (uppercase). It's the redemption side that's not
normalising the input. Please find what's missing and put it back.

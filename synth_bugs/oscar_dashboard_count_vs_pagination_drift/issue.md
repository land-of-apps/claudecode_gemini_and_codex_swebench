Order count on the dashboard heading doesn't match the rows we can see

What we observed
================

When we apply the voucher-code filter on the dashboard order list,
the heading at the top of the page reports a different number than
what we can actually page through. Concrete example: we filtered by
voucher code "SPRING24" this morning. Heading said "47 orders".
Pagination at the bottom said "Page 1 of 5" with 10 orders per
page. We clicked through every page and counted 38 orders.
38 ≠ 47.

Without the voucher filter applied, the heading and the rows agree.
With other filters (status, date range, customer name) the heading
and rows agree. It's specifically when the voucher filter is on
that the numbers drift.

Different staff users see different gaps. One person filtered by
the same voucher and saw "53 orders" with about 41 actual rows.
Another saw "29 / 22". The bigger the gap, the more recently we
ran a campaign with that code.

Things we ruled out
===================

- Not a permissions issue. We ruled out by reproducing as a
  full-admin user.
- Not stale browser caching. Curl with a fresh session shows the
  same drift.
- Not a search-index thing. The filter goes through the regular
  ORM, no Haystack/Solr involved.
- The orders themselves exist and are correct. They just appear
  to be "counted differently" than they're "rendered."

What we expect
==============

When we filter by voucher code (or anything else), the heading
count should equal the number of orders we can actually page
through. If 38 orders match, the heading should say 38, not 47.

We don't think this is a duplicate-orders issue (the orders ARE
distinct in the database — same query, distinct order numbers).
Something between the count and the iteration is treating the
data differently. Please find what it is and put them on the
same footing.

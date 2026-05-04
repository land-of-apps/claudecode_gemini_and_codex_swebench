Basket page is slow when the basket has many items

What we observed
================

The basket page is getting noticeably slow as customers add more
products. With a handful of items it's snappy, but a basket with
20-30 items takes 3-4 seconds to render, sometimes more on a busy
server. We can reproduce in staging without any cache or external
services in the picture — same data, just rendering the basket page.

Browser dev tools show a single request taking that long, so it's
not multiple round-trips client-side. Server-side timing logs
suggest something is happening *per line item* during the render —
the time scales roughly with the line count.

Steps to reproduce
==================

1. Start the development server, log in as a customer.
2. Add 20 different products to the basket (each with a stockrecord,
   ideally with a couple of attributes and a primary image).
3. GET /basket/ and observe the response time.
4. Compare to a basket with 2-3 products — that one is fast.

The math: a 25-item basket should not be ~10x slower than a 3-item
basket if the work is just "render this basket." The data we're
loading per line is bounded.

What we expect
==============

Loading and rendering a basket should be roughly constant-time in
the number of lines, not linear in it. We don't have any per-line
external API calls or anything that could legitimately scale that
way; this is purely an internal data-access shape problem.

We don't have a clean smaller repro than the staging shop yet —
involves the basket model, the lines queryset, and whatever the
basket page renders per line. Please find the cause and fix it so
that adding the 30th item to a basket doesn't pay 30 round-trips.

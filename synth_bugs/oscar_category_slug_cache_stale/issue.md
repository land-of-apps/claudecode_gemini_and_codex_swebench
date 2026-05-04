Renamed a category in the dashboard, customer-facing URLs are still using the old slug

What we observed
================

Last week we renamed a top-level category in the dashboard, from
"Books" to "Books & Magazines" — slug went from `books` to
`books-and-magazines`. The dashboard reflects the new name and the
new slug correctly. We can browse the category list in the admin
and everything looks right.

But when we visit the category page on the public-facing storefront
— or click into the category from anywhere on the site — the URLs
still use `/catalogue/category/books_<id>/`. The breadcrumbs render
the old slug. Sitemaps generated overnight still have the old slug.
Children of the category are similarly stale: `/books/fiction_<id>/`
when it should be `/books-and-magazines/fiction_<id>/`.

Refreshing doesn't help. New incognito sessions don't help. SEO is
already complaining because we have inbound links and Google's
recrawl has dropped the canonical URLs.

Workaround we tried
===================

Restarting the web tier fixes the URLs immediately for everyone.
But within a few hours of normal traffic, the old slugs creep
back in for visitors of the category page — because the page
somehow re-stores the old slug and serves it.

Steps to reproduce
==================

1. Create a category "Test Cat" with slug "test-cat", with a couple
   of children also under reasonable slugs.
2. Visit the category's public page (or render its breadcrumbs in
   any context — anything that reads `category.full_slug`).
3. Rename the category in the dashboard: change "test-cat" to
   "test-cat-renamed".
4. Re-render the page — the URL still uses `test-cat`.
5. Restart the worker — URLs update.
6. After more traffic, the URLs revert to `test-cat`.

What we expect
==============

A rename in the dashboard should immediately reflect in the URLs
served to customers. We're confident the database is updated
correctly (the dashboard reads the new value). It's the URL
rendering layer that's somehow holding on to the old slug.

We don't know if this is a Django middleware thing, a model
property issue, or something specific to how oscar generates the
category URLs. Please find what's holding onto the old data and
make it stop.

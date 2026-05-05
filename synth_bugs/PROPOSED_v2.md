# Proposed v2 fixtures — chosen to test the symptom-to-location hypothesis

After running the first six fixtures (see `runs/comparison_6way.md`),
my picture of "what makes a bug hard for an LLM solver" updated.
The original difficulty axis I used in `PROPOSED.md` (auth vs perf vs
signal etc.) didn't predict the comparison results — four of the six
fixtures collapsed to "easy" because the symptom directly named the
buggy function. The two that genuinely separated the backends
(`oscar_4016`, `oscar_n1_basket`) had a meaningful **symptom-to-location
gap** — the symptom plausibly belonged to a layer other than where the
bug actually lived.

These v2 candidates are designed against that hypothesis. Each has:

  - A user-POV symptom that names a *behavior*, not a *function*.
  - At least 3 plausible code paths the symptom could originate from.
  - The bug planted in the *non-canonical* layer (not the first one
    a developer would reach for).
  - A small, well-defined fix (5-15 lines) so the bug is *tractable*.
  - Runtime evidence (call order, exception flow, SQL pattern, cache
    miss/hit) that points at the right layer when AppMap is used.

If 3-step solver wins decisively on these (and vanilla either fails
or over-fixes), the symptom-to-location-gap hypothesis is validated.
If 3-step doesn't pull ahead, the hypothesis is wrong and I should
revisit.

---

## 1. `oscar_basket_lock_release_on_exception` *[selected for build]*

**user-POV `issue.md` sketch**

> Customers are reporting that after a payment failure they can't
> modify their basket — the page shows their items but the +/-/remove
> buttons don't work, and adding products from elsewhere on the site
> silently does nothing. They have to clear cookies and start a new
> session.
>
> We can reproduce: pick a card that we know our gateway will reject
> with a payment error (not a card-decline — a server-side gateway
> failure), get the "we couldn't take payment" screen, then try to
> change the basket. The basket is somehow stuck.

**where the bug lives** — `checkout/views.py`'s `submit()` method
freezes the basket before payment, then has five `except` branches
that should each call `restore_frozen_basket()` to thaw on failure.
The `except PaymentError` branch is missing that call (one-line
removal). The other four branches handle their cleanup correctly.

**why interesting** —

  - **Symptom names the basket** ("can't modify the basket"); bug
    lives in checkout exception handling. Two layers between symptom
    and cause.
  - **Plausible-but-wrong locations:** Basket model freeze/thaw logic;
    permission/access check on basket views; session storage; basket
    middleware. The actual bug is none of those.
  - **AppMap value is high:** the runtime trace shows `freeze()` fires,
    the PaymentError fires, but `restore_frozen_basket()` doesn't —
    immediately visible in a call tree, invisible from grep.
  - **Tractable:** 1-line fix once the right branch is identified.

**verify** — set up a checkout, mock `handle_payment` to raise
PaymentError, call `submit()`, assert basket.status is OPEN (not
FROZEN).

---

## 2. `oscar_category_slug_cache_stale` *[selected for build]*

**user-POV `issue.md` sketch**

> We renamed a top-level category in the dashboard ("Books" →
> "Books & Magazines"). The change saved fine — the dashboard shows
> the new name and the new slug. But customer-facing URLs are still
> using the old slug for that category and all its descendants.
> Refreshing doesn't help. New incognito sessions don't help. Even
> after some time has passed, the URLs are still wrong.
>
> Restarting the web tier fixes it temporarily; the issue comes back
> the next time anyone (us or a customer) visits a category page,
> because the page somehow re-stores the old slug.

**where the bug lives** — `Category.save()` does not invalidate the
URL slug cache. The cache is populated by `get_full_slug()` on first
access, lives in the Django cache forever-ish, and there's no
`cache.delete(self.get_url_cache_key())` in the save path. *(Note:
this is genuinely under-developed in real oscar — for the fixture I
modify clean upstream to ADD the invalidation, then bug.patch
removes that addition.)*

**why interesting** —

  - **Symptom names URL rendering / the dashboard view.** Plausible
    locations: Category serializer, URL routing, view's
    queryset, template rendering of breadcrumbs.
  - **Bug location is two layers away:** in the model's save path,
    invisible from any view-layer reading.
  - **AppMap value is high:** a recording of "rename category, then
    visit category page" shows the cache HIT returning the stale
    value. `find_calls --method get_full_slug` reveals the cache
    branch firing. Without runtime data, an agent has to know to
    look for cached methods.
  - **Tractable:** 1-2 line fix (add `cache.delete(...)` in save).

**verify** — create a category, access `full_slug`, change the slug,
save, access `full_slug` again, assert it reflects the new slug.

---

## ~~3. `oscar_dashboard_view_signal_skipped_on_redirect`~~ — withdrawn

Initial draft mentioned in issue.md *"the missing orders correlate
with payment paths that go through 3DS or PayPal redirect. Direct-
card-payment orders are tracked correctly."* That's the same shape
of comparative cue ("X works but Y doesn't") that gave vanilla
basket_lock for free. The bug would be solvable by grep on
`view_signal` once the prose is read. Wouldn't separate the
backends — drop.

## ~~4. `oscar_form_clean_method_polymorphism_drift`~~ — withdrawn

Issue.md had to name "subclass of the basket voucher form" as part
of the symptom (otherwise the bug doesn't manifest). That's a
direct layer cue; vanilla resolves it via grep on `BasketVoucherForm`
plus reading the surrounding methods. Plus the test infrastructure
has to ship a hypothetical project-side subclass, which doubles as
an editable target for the agent. Drop.

---

After running basket_lock (where I was wrong about its difficulty)
and cache_stale (where I was right), here's what actually predicts
3-step's win on a fixture:

  - The issue.md prose suggests a *concept* but no *layer*
    (e.g., "URLs go stale, restart fixes" → caching, but
    which cache?).
  - Multiple plausible code paths could produce the symptom.
  - The bug location is buried where domain vocabulary doesn't
    point (e.g., `Category.save()` for a "URL is wrong" bug).

Replacing #3 and #4 with two candidates that fit that pattern.

---

## 3. `oscar_email_total_mismatch` *[selected for build]*

**user-POV `issue.md` sketch**

> Customer service is fielding complaints that the total on the
> order confirmation email doesn't match the total shown on the
> order detail page when customers log back in. We've been able
> to reproduce: place an order with multiple line items and any
> percentage discount applied, the email shows one number, the
> account page shows a different one. The difference isn't a
> rounding penny — it's larger, more like the size of the tax
> portion. The basket page shows the same total as the order
> detail page (correct). Just the email is wrong. We've checked
> the database; the order's total fields are populated correctly.
> So we don't think it's a save-time issue.

**where the bug** — `OrderDispatcher.send_order_placed_email_for_user`
(or whatever the email-rendering hook is in our oscar version)
constructs the template context. The bug: `extra_context['total']`
is set to `order.total_excl_tax` instead of `order.total_incl_tax`.
The order detail page renders `order.total_incl_tax` directly. Same
data; different representation.

**why interesting** —

  - **Prose names "total mismatch", "email" and "order detail page".**
    No code identifier, no method name, no template name. Vanilla's
    natural first move is to look at the email template — but the
    template is correct (it just renders whatever `total` is passed).
  - **Plausibly-wrong locations:** email template, OrderDispatcher,
    the email-template signal handler, the model's `total` property.
    Actual location: a single misnamed attribute access in the
    extra_context dict.
  - **AppMap value:** the recording captures the actual VALUES being
    passed to the template render. `find_calls --method
    send_order_placed_email_for_user` shows the kwargs at a glance;
    `total_excl_tax` vs `total_incl_tax` is visible. Without runtime
    data, the agent has to read every layer of the email path.
  - **Tractable:** 1-line fix.

**verify** — render the email for an order with a discount + tax,
parse the rendered total out of the email body, assert it equals
`order.total_incl_tax`.

---

## 4. `oscar_dashboard_count_vs_pagination_drift` *[selected for build]*

**user-POV `issue.md` sketch**

> The dashboard order list says "234 orders" in the page heading,
> but if you click through to the last page, you only get to about
> 211 actual orders. The discrepancy isn't pagination math — page
> sizes are right, the page numbers count up correctly. Some orders
> are simply missing from the rendered list while still counted in
> the heading. We've ruled out filters: no date filter, no status
> filter, no search query, just a plain list of all orders. The
> count says 234, the rows show 211. Different staff users see
> different gap sizes.

**where the bug** — the count uses one queryset (or counts via
`.aggregate(...)` or `.count()`) and the iterating list uses a
slightly-different queryset. Plantable in a few places:

  - `get_queryset()` returns one queryset, but `get_context_data()`
    counts a different one.
  - The list applies `.distinct()` to drop duplicates from a join
    but the count doesn't.
  - The list applies a `select_related()` that side-effects
    filtering somehow.

I'll pick the `distinct()` divergence as the cleanest plant: the
count goes through `.count()` on the base queryset; the list goes
through `.distinct().count()` (or vice-versa). With a `lines__partner`
filter that creates duplicate rows per partner, the two counts
disagree.

**why interesting** —

  - **Pure behavioral symptom.** No layer name, no identifier, no
    diagnostic implication beyond "numbers disagree."
  - **Plausibly-wrong locations:** the count, the list view, the
    paginator, the queryset, the partner filter.
  - **AppMap value:** SQL recording shows the TWO different SELECT
    queries — one with DISTINCT, one without — issued for the same
    page render. Immediately obvious from `find_queries`. Without
    runtime data, agent has to reason through Django queryset
    semantics.
  - **Tractable:** 1-2 line fix once the divergence is identified.

**verify** — generate a small set of orders with multi-partner
lines (which trigger the duplicate rows on the join), call the
view's get_queryset twice — once for count, once for iteration —
assert they yield the same number of distinct orders.

---

## Rationale for the two picks

`oscar_basket_lock_release_on_exception` and
`oscar_category_slug_cache_stale` are the strongest tests of the
symptom-to-location hypothesis because both have:

  - Clear customer-facing symptoms (basket frozen, URLs stale)
  - Symptoms that name a layer 2+ removed from the actual bug
  - A small fix (1-2 lines) so the bug is tractable
  - Runtime evidence that sharply distinguishes correct from buggy
    behavior — exception trace for #1, cache hit/miss patterns for #2

If 3-step wins by 30%+ on cost and vanilla fails or over-fixes
on either of these, the hypothesis is supported. If both backends
solve them equally easily, the hypothesis needs revision — perhaps
the LLM is better at non-grep-friendly bugs than I credit.

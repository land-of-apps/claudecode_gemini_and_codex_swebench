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

## 3. `oscar_dashboard_view_signal_skipped_on_redirect` *[deferred]*

**user-POV `issue.md` sketch**

> We have an analytics receiver attached to oscar's view_signal that
> tracks "thank you page reached" events for funnel analysis. About
> 12% of orders are missing from the funnel — we know they happened
> (the order is in the DB, the customer got the email) but the
> "thank you reached" event never fires.
>
> Looking at logs, the missing orders correlate with payment paths
> that go through 3DS or PayPal redirect. Direct-card-payment orders
> are tracked correctly.

**where the bug** — `OrderPlacementMixin.handle_successful_order`
sends `view_signal` after a successful order is placed. But
redirect-required payment paths return early from `submit()` with an
HTTP redirect, never reaching `handle_successful_order`. The
post-redirect return path doesn't fire view_signal.

**why deferred** — the bug is plausible but I'd need to introduce a
view_signal-aware analytics receiver to make the test work, plus a
mock 3DS redirect path. Plantable but more setup than the first two.

---

## 4. `oscar_form_clean_method_polymorphism_drift` *[deferred]*

**user-POV `issue.md` sketch**

> We have a project-level subclass of the basket voucher form that
> adds an extra validation: voucher codes must not contain spaces
> (we generate ours with no spaces and we don't want users to paste
> in malformed codes). The validation worked fine for years.
>
> After upgrading oscar a few weeks back, the validation silently
> stopped firing. Codes with spaces are accepted again. Our
> subclass is unchanged, but our validation method is no longer
> being called from anywhere.

**where the bug** — base class's `clean_code` was renamed (or its
signature changed); the subclass's override now has a different name
relative to the new base, so it's not called by the form-cleaning
machinery. Plantable as a renamed method on the base.

**why deferred** — requires inventing a project-side subclass to
test against, plus the test setup is complex. Would be a great
fixture but more work than the first two.

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

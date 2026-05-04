# Proposed synthetic bug fixtures

This file records candidate synth_bugs fixtures we've discussed but not
yet built. Each entry follows the same shape as `oscar_4016`:

  - Clean upstream tree (post-fix oscar source) at a known location
  - `bug.patch` — re-introduces the bug
  - `verify.patch` — adds a hidden test asserting the fix
  - `issue.md` — symptom-only report from a user's perspective
  - `fixture.json` — references all of the above

Design constraints carried over from `oscar_4016`:

  1. **`issue.md` never names files, methods, or root cause.** It's
     written as if a customer or operator filed it — symptom + steps
     to reproduce + impact. The agent has to find the cause.
  2. **Verify test is hidden** until after patch extraction. We never
     give the agent the spec.
  3. **Bug must be plant-able with a small patch** (1–10 lines is
     ideal). Bigger bugs make fixture maintenance painful.
  4. **The fix must be runtime-observable** in some way AppMap can
     capture — SQL, call tree, HTTP request, parameters, exceptions,
     or a labeled function. Otherwise we're just benchmarking grep.

The five candidates below span: performance, functional, signal/order-of-
ops, numerical correctness, and authorization. They're roughly ordered
by buildability — easier first.

---

## 1. `oscar_n1_basket` — Performance: N+1 on basket page  *[selected]*

**user-POV `issue.md` sketch**

> The basket page is getting slow as we add more products. With 5
> items it's snappy; with 25 items it takes 3-4 seconds to render.
> Same data load — just rendering the basket page from the API.
> Devtools shows a single request taking that long. Suggests
> something is happening per-line that shouldn't be.

**where the bug lives** — `Basket.all_lines()` (or the basket view's
context-data assembly) returns a queryset without
`select_related('product', 'stockrecord')` / `prefetch_related` for
the product-options lookups. Every template access of
`line.product.title` or `line.stockrecord.price_excl_tax` issues a
fresh SELECT, so cost scales linearly with line count.

**why it's interesting** — Plays directly to AppMap's strength: SQL
recording shows ~3 queries per line. `find_queries --table catalogue_product`
or `sql_hotspots` resolves the diagnosis in one verb. A grep-only
agent has to reason about Django querysets and template access
patterns without runtime data, which is harder. The benchmark gap
between AppMap-driven and base claude should be largest here.

**difficulty** — Easy *with* AppMap, medium without. Patch is 2-3
lines (add `.select_related(...)` to one method).

**verify** — Test that asserts a basket with N products renders in
≤ K queries (where K is small and constant in N). Use
`django.test.utils.CaptureQueriesContext` or `assertNumQueries`.

---

## 2. `oscar_voucher_case` — Functional: voucher code case sensitivity

**user-POV `issue.md` sketch**

> We email customers voucher codes in our newsletter — codes are
> usually lowercase like "spring24" because that's what marketing
> types in. When customers paste the code into the basket form they
> get "Voucher not found." If they manually retype it in caps
> ("SPRING24") it works. The dashboard says voucher creation is
> case-insensitive but redemption clearly isn't.

**where the bug lives** — `BasketVoucherForm.clean_code` uses
`Voucher.objects.get(code=code)` instead of `code__iexact=code`.
The model's manager has an `_active_voucher` lookup that uses
`iexact`, but the form bypassed it.

**why it's interesting** — Tests cross-layer investigation (form →
model → manager). AppMap's HTTP recording catches the form POST and
the failing query; the SQL trace shows the case-sensitive WHERE
clause. Easy to verify.

**difficulty** — Medium. Patch is 1 line.

**verify** — Form submitted with `"SPRING24"` matches a voucher
created with code `"spring24"`.

---

## 3. `oscar_dup_order_email` — Functional: duplicate order confirmation email  *[selected]*

**user-POV `issue.md` sketch**

> Customers are reporting they get two order confirmation emails for
> the same order — same subject, same content, same order number —
> about a second apart. Our support inbox is filling up with confused
> replies. We have one signal handler attached to `order_placed` that
> sends the email; we double-checked and it's only registered once.
> Yet two go out.

**where the bug lives** — `OrderPlacementMixin.handle_order_placement`
(or equivalent in the checkout flow) dispatches the `order_placed`
signal twice — once before the basket-merge step (a legacy code
path), once after the order is fully saved. The first dispatch was
supposed to be removed in a prior cleanup but came back via a merge.

**why it's interesting** — Pure runtime symptom — the agent has no
way to suspect "double signal" from grep alone. AppMap's call-tree
query (`get_call_tree --focus_value send_email_messages` or
`find_calls --method send`) shows two `send_email` invocations from
one POST. Without runtime data, the agent has to read several large
files to spot the double dispatch.

**difficulty** — Medium-hard for grep-only, easier for AppMap.
1-line removal as the fix.

**verify** — A test that places an order through the checkout view
and asserts `len(mail.outbox) == 1`.

---

## 4. `oscar_discount_rounding` — Correctness: rounding drift in percentage discount

**user-POV `issue.md` sketch**

> We're seeing reconciliation errors at month-end — checkout total
> and receipt total drift by 1-2p (cents). Doesn't happen on every
> order, but on orders with multiple line items and a percentage
> discount applied across the whole basket. The shopper sees the
> right number on the basket page; the receipt PDF and the order
> detail page disagree by a penny. Finance is unhappy.

**where the bug lives** — `PercentageDiscountBenefit.apply` rounds
each line's discount independently before summing, instead of
computing the total discount and distributing rounding correction.
Classic Decimal accumulation bug.

**why it's interesting** — Numerical bugs are notoriously hard to
debug from source alone — you need to see *the actual values* at
runtime. AppMap's parameter capture (with labels on the discount-
applying methods) makes the drift visible. This is where the
@label workflow earns its keep.

**difficulty** — Hard. Subtle. Patch is ~5-10 lines (proper
distribution algorithm). May be too hard for a one-shot fix; useful
as a stretch test once the easier ones are landing reliably.

**verify** — A test that constructs a 3-line basket with a 33%
discount and asserts the order total equals the sum of line
discounted totals to the penny.

---

## 5. `oscar_partner_orders_leak` — Authorization: cross-partner order visibility

**user-POV `issue.md` sketch**

> A partner reported they can see orders from a different partner on
> their dashboard. We have multi-tenant partners and each partner's
> staff should only see their own orders. The partner is using the
> standard staff dashboard URL (not anything custom). We've checked
> permissions in the admin and they look right — partner staff are in
> the right groups. Yet the orders list shows orders that don't
> belong to them.

**where the bug lives** — `DashboardOrderListView.get_queryset`
filters by `is_staff=True` but doesn't apply the partner restriction;
the partner restriction lives in a mixin that's only applied to
*some* dashboard views.

**why it's interesting** — Authorization bugs typically require the
agent to understand both the view-layer and the data-model
relationship. AppMap's HTTP recording with parameter capture shows
the queryset returning wrong rows. Verify is straightforward (a test
that creates two partners and asserts staff of partner A can't see
partner B's orders).

**difficulty** — Hard. Multi-file investigation. ~3-5 line patch
(apply the right mixin or add the missing filter).

---

## Selected for build (next)

1. **`oscar_n1_basket`** — the cleanest test of "AppMap pays for
   itself on perf bugs." Comparison gap should be largest here, and
   the bug is easy to plant.

2. **`oscar_dup_order_email`** — different shape from `oscar_4016`
   (signal/dispatch rather than logic). Exercises the call-tree
   query verb specifically. Hard to grep, easy to recording-trace.

These two complement `oscar_4016` (which is a logic bug surfaced
by call structure). Together they give us three different bug
*shapes* and three different reasons AppMap is useful.

Build order:
- `oscar_n1_basket` first — easier to plant (existing oscar source
  already has the relevant queryset; just need to construct the
  reverse-of-fix patch that strips `select_related`).
- `oscar_dup_order_email` second — needs a slightly larger patch
  but still well under 10 lines.

After both land, re-run the smoke matrix (claude / claude-appmap-mcp
/ claude-appmap-3step × 3 fixtures) to see if the cost / quality
gradient holds across bug shapes.

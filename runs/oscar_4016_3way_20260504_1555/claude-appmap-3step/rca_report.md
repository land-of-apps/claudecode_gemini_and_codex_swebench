## Root cause

`LineOfferConsumer.available()` in `src/oscar/apps/basket/utils.py` uses an asymmetric tie-breaker that fails to block a subsequent exclusive offer when an earlier exclusive offer (already consumed on the line) has a *higher* primary key. For two exclusive voucher offers with equal `priority` (the default 0), the inner check at lines 164-167 reduces `max_affected_items` only when `a.priority > offer.priority` OR `a.priority == offer.priority AND a.id < offer.id`. When the already-applied offer's `id` is GREATER than the offer under evaluation, neither clause is true, the loop falls through without zeroing `max_affected_items`, and `available()` returns the full line quantity. The second exclusive offer then applies on top of the first. Whether this happens depends on `basket.vouchers.all()` ordering, which is why it only reproduces when "voucher two" (whose offer has the higher id) is added before "voucher one".

## Evidence

- Recording: `Exclusive voucher offers two exclusive voucher offers should not both apply` — both `OfferApplications#add` (events 271, 451) fire; `BasketDiscount.is_successful` returns True for both offers' `apply_benefit`.
- Call path: `Applicator#apply_offers` (`src/oscar/apps/offer/applicator.py:26`) → `AbstractConditionalOffer#apply_benefit` (`src/oscar/apps/offer/abstract_models.py:300`) → `PercentageDiscountBenefit#apply` (`src/oscar/apps/offer/benefits.py:56`) → `AbstractLine#quantity_without_offer_discount` → `LineOfferConsumer#available` (`src/oscar/apps/basket/utils.py:146`).
  - On the SECOND offer's evaluation, `applied` contains the first offer (verified — combinations SQL queries fire for both `from_conditionaloffer_id=1` and `=2`, indicating `applied=[A]` reaches the post-exclusive `for x in applied` loop). The exclusive-vs-exclusive branch did NOT return 0.
- Direct runtime probe (post-`Applicator().apply(...)`): `applications=['OB','OA']`; `consumer._consumptions={2:1, 1:1}` — both offers consumed the single line. With `A.id=1, B.id=2`, both `priority=0`: when evaluating A with `applied=[B]`, `b.id < a.id` is `2 < 1 = False`, so the `if any([...])` predicate is False and no reduction occurs.

## Files / lines

- `src/oscar/apps/basket/utils.py:161-172` — the offending block:
  ```python
  if offer.exclusive:
      for a in applied:
          if a.exclusive:
              if any([
                  a.priority > offer.priority,
                  a.priority == offer.priority and a.id < offer.id
              ]):
                  max_affected_items = max_affected_items - self.consumed(a)
                  if max_affected_items == 0:
                      return 0
  ```
  When `a.priority == offer.priority`, the comparison must be symmetric — any other already-applied exclusive offer must block, regardless of id ordering. The fix is to drop the asymmetric id check (or make it symmetric, e.g. always reduce `max_affected_items` for any applied exclusive offer of equal-or-higher priority).
- `src/oscar/apps/basket/utils.py:174-177` — the sibling `else` branch already returns 0 unconditionally for non-exclusive vs exclusive collisions; the symmetric handling is what the equal-priority exclusive case lacks.

## Caveats

- The same asymmetric comparison has the inverse failure mode for non-voucher offers ranked by priority: a strictly-higher-priority exclusive offer correctly blocks, but two exclusive offers with equal priority will deterministically pick the lower-id one to "win" — which today is dressed up as a tie-breaker but actually leaks because the *loser*'s `available()` call is never re-checked from the winner's perspective (the loop is per-applied, not per-pair).
- `Applicator.get_basket_offers` iterates `basket.vouchers.all()` (default ordering by id), so the order vouchers are added drives which offer the applicator processes first; the bug only manifests when the higher-id offer gets processed first.
- `appmap.yml` was modified to add `oscar.apps.offer/basket/voucher` packages so the call tree was visible; revert if that interferes with later steps.
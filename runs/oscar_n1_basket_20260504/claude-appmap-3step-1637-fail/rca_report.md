The evidence is clear-cut. I have everything needed for the report.

The N+1 pattern is confirmed across multiple per-line tables. With 8 lines I see:
- 8x `catalogue_productimage` queries (line `images.all()` via `primary_image()`)
- 8x `basket_lineattribute` queries (`line.description`'s `self.attributes.all()`)
- 16+ `partner_stockrecord` queries (8 in the first pass, 8+ during template render — `Strategy.fetch_for_product` per line, plus stockrecord lookups for line `product`)
- Also implicit per-line `catalogue_product` and `catalogue_productclass` (loaded as `line.product` and `product.product_class`) selects

Total 208 SQL for the request, ~26/line. The root cause is `AbstractBasket.all_lines()` returning a bare `self.lines.all()` queryset — no `select_related`/`prefetch_related` to bring in the per-line relations the template and `purchase_info_for_line` traverse.

## Root cause

`oscar.apps.basket.abstract_models.AbstractBasket.all_lines()` (src/oscar/apps/basket/abstract_models.py:120-133) returns `self.lines.all().order_by(...)` with no `select_related` or `prefetch_related`. The basket summary view (`BasketView.get_queryset`, src/oscar/apps/basket/views.py:50-55) feeds this same queryset into the formset that renders `basket_content.html`. For every line the template touches `line.product`, `line.stockrecord`, `product.primary_image()` (which calls `images.all()`), `line.description` (which iterates `self.attributes.all()`), and the strategy's `fetch_for_product()` (which loads stockrecords). None of those relations are prefetched, so each is fetched per row — the classic N+1 that grows linearly with line count, exactly matching the reported timing.

## Evidence

- Recording: `Basket summary n1 basket summary with many lines` (appmap_id 2) — single GET `/basket/`, 8 basket lines, status 200, 208 SQL queries, 452 ms total.
- Call path: `BasketView.get_queryset` (src/oscar/apps/basket/views.py:55) -> `AbstractBasket.all_lines` (src/oscar/apps/basket/abstract_models.py:131) -> `self.lines.all()` returns a non-optimised queryset that template iteration then walks per row.
- Anomalous SQL (one per line, 8 lines):
  - `SELECT ... FROM "catalogue_productimage" WHERE "product_id" = 1..8` — driven by `Product.primary_image -> get_all_images -> self.images.all()` (src/oscar/apps/catalogue/abstract_models.py:690-708) called from the line image block in basket_content.html:49-54.
  - `SELECT ... FROM "basket_lineattribute" WHERE "line_id" = 1..8` — driven by `AbstractLine.description`'s `self.attributes.all()` (src/oscar/apps/basket/abstract_models.py:877) used at basket_content.html:57.
  - `SELECT ... FROM "partner_stockrecord" WHERE "product_id" = 1..8 LIMIT 1` — driven by the strategy's per-product price/availability fetch via `purchase_info_for_line` (basket_content.html:44) and warning checks in `BasketView.get_basket_warnings` (views.py:71-80).
- The same pattern repeats for `line.product` and `product.product_class` lookups; total cost scales linearly with `len(basket.lines)`.

## Files / lines

- `src/oscar/apps/basket/abstract_models.py:120-133` — `AbstractBasket.all_lines` builds the queryset; this is the single entry point that must add `select_related('product', 'product__product_class', 'stockrecord', 'stockrecord__partner')` and `prefetch_related('attributes', 'attributes__option', 'product__images', 'product__stockrecords')` (and probably the parent's images for child products).
- `src/oscar/apps/basket/abstract_models.py:874-885` — `AbstractLine.description` walks `self.attributes.all()`; needs the prefetch above to be N-free.
- `src/oscar/apps/catalogue/abstract_models.py:690-708` — `Product.primary_image` walks `self.images.all()`; the prefetch on `product__images` removes the per-product hit.
- `src/oscar/apps/basket/views.py:50-55, 71-80` — caller chains; no changes needed there once `all_lines()` returns an optimised queryset.

## Caveats

- The same `all_lines()` is reused for the saved-basket formset (views.py:134) and the order-line/warning paths, so prefetching there fixes both lists at once but be sure the chosen prefetches make sense for both.
- `purchase_info_for_line`/`Strategy.fetch_for_product` reaches `product.stockrecords.all()` and `product.product_class`; both must be in the prefetch/select_related set or the per-line stockrecord/product_class SELECTs will remain.
- Scratch reproducer left at `tests/functional/basket/test_n1_repro.py` — safe to delete; it was only used to record the AppMap.
I have everything I need. The smoking gun is `all_lines()` in `abstract_models.py:120-133` which returns `self.lines.all()` with no `select_related`/`prefetch_related`. With 6 lines we observed:

- Total SQL: 180 (per recording metadata).
- 36 `SELECT FROM partner_stockrecord WHERE product_id=?` (6 per line × 6 lines, called from `select_stockrecord` at strategy.py:198).
- Multiple call paths driving it: `purchase_info` -> `fetch_for_line` -> `fetch_for_product` -> `select_stockrecord` -> `product.stockrecords.all()[0]`. Hit during `get_basket_warnings` (line.get_warning), template render (`basket_content.html` `purchase_info_for_line`, `unit_price_incl_tax`, `is_tax_known`, etc.).

## Root cause

`Basket.all_lines()` (src/oscar/apps/basket/abstract_models.py:120-133) returns `self.lines.all().order_by(...)` with no `select_related`/`prefetch_related`. The basket render path then iterates lines and, for each line, calls `line.purchase_info` which calls `strategy.fetch_for_line(line, line.stockrecord)`. Both `line.stockrecord`/`line.product` (FKs) and `UseFirstStockRecord.select_stockrecord` (`product.stockrecords.all()[0]` at src/oscar/apps/partner/strategy.py:198) issue per-line queries. Result: query count grows linearly with line count — exactly the symptom in the report.

## Evidence

- Recording: `Basket render n1 test render basket with many lines` (appmap_id=1) — GET `/basket/`, 6 lines, **180 SQL queries**, 401 ms.
- Per-line SQL fingerprints inside the request:
  - 36× `SELECT … FROM "partner_stockrecord" WHERE "partner_stockrecord"."product_id" = ?` (6 lines × 6 calls = ~6 stockrecord lookups per line).
- Call path (parent chain captured for one stockrecord SELECT):
  `BasketView.get_context_data → get_basket_warnings → AbstractLine.get_warning → AbstractLine.purchase_info → Base.fetch_for_line → Structured.fetch_for_product → UseFirstStockRecord.select_stockrecord → SQL`
  and again on the template render path:
  `<basket_content.html>.render → Base.fetch_for_line → Structured.fetch_for_product → UseFirstStockRecord.select_stockrecord → SQL`
- 12 distinct `select_stockrecord` invocations occurred during the single `/basket/` request (2 per line × 6 lines), each triggering its own `product.stockrecords.all()[0]` SQL.

## Files / lines

- `src/oscar/apps/basket/abstract_models.py:120-133` — `all_lines()` returns `self.lines.all()` with no eager loading. This is the single root-cause site: it should `select_related('product', 'stockrecord')` and `prefetch_related('product__stockrecords', 'product__images', 'attributes')` so the per-line accessors that follow are cache hits.
- `src/oscar/apps/basket/abstract_models.py:798-807` — `AbstractLine.purchase_info` reads `self.stockrecord` (FK) and dispatches to `strategy.fetch_for_line(self, self.stockrecord)`; without prefetch, this is one query per line.
- `src/oscar/apps/partner/strategy.py:194-200` — `UseFirstStockRecord.select_stockrecord` does `product.stockrecords.all()[0]`. The comment claims "no additional queries when stockrecords have already been prefetched" — but `all_lines()` does not prefetch them, so this is exactly where the N+1 fires.
- `src/oscar/templates/oscar/basket/partials/basket_content.html:42-119` — per-line template uses `product.primary_image`, `product.get_absolute_url`, `purchase_info_for_line`, `line.unit_price_incl_tax`, `line.is_tax_known`, `line.line_price_incl_tax` — every one of these traverses uncached relations or recomputes `purchase_info`.

## Caveats

- The same N+1 pattern also fires in `BasketView.get_basket_warnings` (views.py:75-80) before the template even renders, so fixing only the template will not eliminate it.
- The "saved basket" branch in the same template (`basket_content.html:189-226`) iterates `saved_formset` and accesses `form.instance.product.primary_image`, etc.; `BasketView.get_saved_basket_queryset` (views.py:510) also uses `saved_basket.all_lines()` — fix to `all_lines()` will benefit both.
- A scratch reproducer was added at `tests/functional/test_basket_n1.py` (6-line basket, GET /basket/). Not needed by the fix; safe to delete.
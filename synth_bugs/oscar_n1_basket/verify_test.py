"""Hidden verification test — never lives in the agent's repo.

The basket-page N+1 fix must make `Basket.all_lines()` issue a
constant number of queries regardless of how many lines the basket
holds. We assert that property by comparing the query count for
a small basket vs a large one — under the bug they differ
substantially; under the fix they are within noise.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from oscar.test.factories import BasketFactory, ProductFactory


def _render_query_count(basket):
    """Force fresh lines load and access every per-line attribute the
    basket page would touch — product, stockrecord, attributes."""
    basket._lines = None
    with CaptureQueriesContext(connection) as ctx:
        for line in basket.all_lines():
            _ = line.product.title
            _ = line.stockrecord.price
            _ = list(line.attributes.all())
    return len(ctx.captured_queries)


@pytest.mark.django_db
def test_basket_lines_render_with_bounded_queries():
    products = [ProductFactory(stockrecords__price=10) for _ in range(10)]

    basket_small = BasketFactory()
    for p in products[:2]:
        basket_small.add_product(p, quantity=1)

    basket_large = BasketFactory()
    for p in products:
        basket_large.add_product(p, quantity=1)

    qc_small = _render_query_count(basket_small)
    qc_large = _render_query_count(basket_large)

    # With proper prefetching, query count is independent of line count
    # (one per relation, not one per line per relation). Without
    # prefetching, qc grows linearly with line count: ~1 + k * N for
    # k accesses per line. A 5x line-count increase should cost at
    # most a handful more queries — anything more is the N+1 pattern.
    assert qc_large - qc_small < 5, (
        f"Basket render queries scale with line count: "
        f"{qc_small} for 2 lines, {qc_large} for 10 lines. "
        f"This is the N+1 pattern — Basket.all_lines() (or whatever "
        f"the basket view loads lines through) is missing prefetch_related "
        f"or select_related on the per-line accesses."
    )

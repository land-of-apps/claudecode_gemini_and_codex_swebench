"""Hidden verification test — never lives in the agent's repo.

Renaming a category should invalidate cached URL slugs so that
subsequent reads of `full_slug` reflect the new value. The bug
under test leaves the cache populated with the pre-rename slug;
visitors continue to see the old URL until the cache TTL expires
or the cache is flushed manually.

Two scenarios are exercised:

  1. Renaming a child (non-root) category should invalidate that
     category's own cache.
  2. Renaming a parent should also invalidate every descendant's
     cache, since each descendant's full_slug embeds the parent's
     slug.

Note: root categories bypass the cache in `get_full_slug` (early
return on `is_root()`), so a root-only rename is not a meaningful
test of cache invalidation. Both assertions below involve at
least one non-root cache.
"""

import pytest

from django.core.cache import cache
from oscar.apps.catalogue.models import Category


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_renaming_child_category_invalidates_full_slug_cache():
    parent = Category.add_root(name="Books", slug="books")
    parent = Category.objects.get(pk=parent.pk)
    child = parent.add_child(name="Fiction", slug="fiction")
    child = Category.objects.get(pk=child.pk)

    # Prime the cache.
    assert child.full_slug == "books/fiction"

    # Rename the child and save.
    child.slug = "fiction-novels"
    child.save()

    fresh_child = Category.objects.get(pk=child.pk)
    assert fresh_child.full_slug == "books/fiction-novels", (
        f"After renaming child slug to 'fiction-novels', full_slug "
        f"returned {fresh_child.full_slug!r}. The URL cache populated "
        f"by the first read was not invalidated when the child's slug "
        f"changed."
    )


@pytest.mark.django_db
def test_renaming_parent_invalidates_descendants_full_slug_cache():
    parent = Category.add_root(name="Books", slug="books")
    parent = Category.objects.get(pk=parent.pk)
    child = parent.add_child(name="Fiction", slug="fiction")
    child = Category.objects.get(pk=child.pk)

    # Prime caches for both. (Root bypass means the parent's own
    # `full_slug` is uncached anyway, so the parent priming is
    # really a no-op — the child priming is what matters.)
    assert parent.full_slug == "books"
    assert child.full_slug == "books/fiction"

    # Rename the parent.
    parent.slug = "books-and-magazines"
    parent.save()

    fresh_child = Category.objects.get(pk=child.pk)
    assert fresh_child.full_slug == "books-and-magazines/fiction", (
        f"After renaming parent category, child full_slug is "
        f"{fresh_child.full_slug!r}. Renaming a parent must invalidate "
        f"every descendant's URL cache, since each descendant's "
        f"full_slug embeds the parent's slug."
    )

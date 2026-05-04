"""Reproduce oscar issue 4016 by driving a real browser against a live runserver.

Each click goes through the actual middleware → view → response cycle, so
AppMap's Django HTTP middleware records each request as a normal HTTP
recording (one per page navigation / form submit).

Why Playwright over the Django test Client:
  - Real CSRF token extraction from the form
  - Real form-field discovery (no hardcoded field names)
  - Closer to the bug report's "user adds a voucher in the basket page"

Invoked by bin/repro.sh, which is responsible for:
  - starting `manage.py runserver` in the background
  - waiting for it to listen
  - calling this script
  - tearing down the server after
"""

import os
import sys
import time
from decimal import Decimal
from datetime import timedelta

import django

django.setup()

from django.utils import timezone
from playwright.sync_api import sync_playwright

from oscar.apps.partner.models import Partner, StockRecord
from oscar.apps.catalogue.models import ProductClass, Product
from oscar.apps.offer.models import ConditionalOffer, Range, Condition, Benefit
from oscar.apps.voucher.models import Voucher

BASE_URL = os.environ.get("REPRO_BASE_URL", "http://127.0.0.1:8000")


def seed():
    """Create the bug context: 1 product + 2 exclusive voucher offers."""
    pc = ProductClass.objects.create(name="Default", slug="default")
    product = Product.objects.create(title="Test product",
                                     product_class=pc, slug="test")
    partner = Partner.objects.create(name="Test", code="test")
    StockRecord.objects.create(product=product, partner=partner,
                               partner_sku="t1", price=Decimal("10.00"),
                               num_in_stock=100)
    rng = Range.objects.create(name="all", includes_all_products=True)

    def voucher_offer(name, code):
        cond = Condition.objects.create(range=rng, type=Condition.VALUE,
                                        value=Decimal("1"))
        bene = Benefit.objects.create(range=rng, type=Benefit.PERCENTAGE,
                                      value=Decimal("10"))
        offer = ConditionalOffer.objects.create(
            name=name, condition=cond, benefit=bene,
            offer_type=ConditionalOffer.VOUCHER,
            exclusive=True,
            start_datetime=timezone.now() - timedelta(days=1),
            end_datetime=timezone.now() + timedelta(days=30),
        )
        v = Voucher.objects.create(
            name=name, code=code,
            usage=Voucher.MULTI_USE,
            start_datetime=timezone.now() - timedelta(days=1),
            end_datetime=timezone.now() + timedelta(days=30),
        )
        v.offers.add(offer)

    voucher_offer("offer1", "VOUCHER1")
    voucher_offer("offer2", "VOUCHER2")
    return product


def main():
    print(f"[repro] seed: 1 product, 2 exclusive offers, 2 voucher codes")
    product = seed()
    product_url = f"{BASE_URL}/en-gb/catalogue/{product.slug}_{product.pk}/"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context()
        page = ctx.new_page()

        print(f"[repro] visit product: {product_url}")
        page.goto(product_url, wait_until="domcontentloaded")

        print("[repro] add to basket")
        page.locator("form#add_to_basket_form button[type=submit]").first.click()
        page.wait_for_load_state("domcontentloaded")

        for code in ("VOUCHER2", "VOUCHER1"):
            print(f"[repro] apply {code}")
            page.goto(f"{BASE_URL}/en-gb/basket/", wait_until="domcontentloaded")
            # The voucher form lives inside #voucher_form_container which oscar
            # ships with style="display:none"; the toggle is a JS handler bound
            # to #voucher_form_link → unbind by setting display directly.
            page.evaluate(
                "document.querySelector('#voucher_form_container')"
                ".style.display='block'"
            )
            page.locator("input[name=code]").fill(code)
            page.locator("form[action*='vouchers/add'] button[type=submit]").first.click()
            page.wait_for_load_state("domcontentloaded")

        print("[repro] read basket page after both voucher submits")
        page.goto(f"{BASE_URL}/en-gb/basket/", wait_until="domcontentloaded")
        body = page.locator("body").inner_text()

        # The basket page lists each applied voucher in the "Vouchers" panel
        # — count visible occurrences of each code as evidence both attached.
        v1_visible = "VOUCHER1" in body
        v2_visible = "VOUCHER2" in body

        # The bug-fix path emits an error message when applying the second
        # exclusive voucher. Look for both directions.
        error_hints = ("cannot be used", "is already applied", "exclusive")
        had_error = any(h.lower() in body.lower() for h in error_hints)

        browser.close()

    print()
    print(f"[repro] VOUCHER1 visible on basket page: {v1_visible}")
    print(f"[repro] VOUCHER2 visible on basket page: {v2_visible}")
    print(f"[repro] exclusivity error message present: {had_error}")

    # Bug REPRODUCED if both vouchers stuck and no exclusivity error fired.
    if v1_visible and v2_visible and not had_error:
        print("[repro] BUG REPRODUCED — both exclusive vouchers attached, no error")
        sys.exit(0)
    else:
        print("[repro] no bug — second voucher rejected or error shown")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Hidden verification test — never lives in the agent's repo.

Asserts that `queryset_orders_for_user(user)` for a non-staff user
who belongs to one Partner returns ONLY orders whose lines reference
that partner. The bug under test allows partner-staff users to see
every partner's orders.
"""

import pytest

from oscar.apps.dashboard.orders.views import queryset_orders_for_user
from oscar.test.factories import (
    OrderFactory,
    OrderLineFactory,
    PartnerFactory,
    UserFactory,
)


@pytest.mark.django_db
def test_partner_staff_only_see_orders_for_their_own_partner():
    partner_a = PartnerFactory(name="Acme Trading")
    partner_b = PartnerFactory(name="Beta Goods")

    # Partner staff are NOT is_staff in the auth-system sense; they get
    # dashboard access via Partner.users membership and oscar's own
    # permission decorators.
    user_a = UserFactory(is_staff=False)
    partner_a.users.add(user_a)

    order_for_a = OrderFactory()
    OrderLineFactory(order=order_for_a, partner=partner_a)

    order_for_b = OrderFactory()
    OrderLineFactory(order=order_for_b, partner=partner_b)

    visible_numbers = set(
        queryset_orders_for_user(user_a).values_list("number", flat=True)
    )

    assert order_for_a.number in visible_numbers, (
        "user_a (staff of partner A) cannot see partner A's order — "
        "the partner filter is over-restrictive."
    )
    assert order_for_b.number not in visible_numbers, (
        f"user_a (staff of partner A only) can see order "
        f"{order_for_b.number} which belongs to partner B. "
        f"The dashboard order list is leaking orders across partner "
        f"tenancy boundaries."
    )

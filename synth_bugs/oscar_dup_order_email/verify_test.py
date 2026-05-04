"""Hidden verification test — never lives in the agent's repo.

Asserts that `OrderPlacementMixin.handle_successful_order` triggers
exactly one `send_order_placed_email` call per order, regardless of
whether the early "defensive" send was retained, removed, or
restructured. Mocks the inner email-send method so the test doesn't
depend on an email backend or a real SMTP path.
"""

from unittest.mock import MagicMock, patch

import pytest

from oscar.apps.checkout.mixins import OrderPlacementMixin
from oscar.test.factories import OrderFactory


@pytest.mark.django_db
def test_handle_successful_order_sends_one_confirmation_email():
    order = OrderFactory()

    # Build a stripped-down mixin instance with stubs for the request
    # and session — we only care about the email-send count here.
    mixin = OrderPlacementMixin()
    mixin.request = MagicMock()
    mixin.request.session = {}
    mixin.checkout_session = MagicMock()

    # Patch send_signal so we don't pull in the view-signal pathway,
    # and patch get_success_url for the redirect.
    with patch.object(mixin, "send_signal"), \
         patch.object(mixin, "get_success_url", return_value="/checkout/thank-you/"), \
         patch.object(mixin, "send_order_placed_email") as mock_send:
        mixin.handle_successful_order(order)

    assert mock_send.call_count == 1, (
        f"send_order_placed_email was invoked {mock_send.call_count} times "
        f"for a single order; should be exactly 1. Each invocation produces "
        f"a customer-facing confirmation email — duplicates are visible to "
        f"users."
    )

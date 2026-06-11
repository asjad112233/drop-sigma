"""Single source of truth for customer-facing tracking links.

Every customer-visible surface (shipping notification emails, the
tenant's dashboard, the Sourcing Ops back-office, the Sourcing
Partners portal, and any future API that's consumed by the buyer)
must route the tracking link through ``build_ds_tracking_link()``.

The function produces the Drop Sigma tracking page URL
(`https://track.dropsigma.com/<TRACKING_NUMBER>/`) — the same page
served by ``tracking_public.views.tracking_detail``. This:

  1. Keeps the carrier (Yuntrack, YunExpress, DHL, …) invisible to
     the customer — the seller's brand owns the entire post-purchase
     experience.
  2. Lets us evolve the rendering (we already mirror the carrier's
     5-stage flow + prepend our 2 platform stages) without re-emailing
     every customer the new URL.
  3. Lets a single ``Order.tracking_number`` flip the link on
     instantly — no per-carrier URL templates required.

The raw carrier URL stays on ``Order.tracking_url`` for internal
admin / debugging / vendor-submission flows, BUT must never reach
the buyer.

If the order has no tracking number yet, the helper returns "".
Callers decide whether to render a placeholder or hide the row.
"""
from __future__ import annotations

from django.conf import settings

# The host where ``tracking_public`` serves the customer-facing
# tracking page. Configurable via Django settings so a future
# white-label rebrand or staging environment can swap it without
# touching every caller.
TRACK_DOMAIN: str = getattr(settings, "DS_TRACK_DOMAIN", "track.dropsigma.com")


def build_ds_tracking_link(order) -> str:
    """Return the customer-facing Drop Sigma tracking URL for ``order``.

    ``order`` is anything with a ``tracking_number`` attribute (typically
    ``orders.models.Order``, but any duck-typed object works).
    Returns the empty string when no tracking number is set — callers
    should not render a broken link.
    """
    if order is None:
        return ""
    tn = (getattr(order, "tracking_number", "") or "").strip()
    return build_ds_tracking_link_from_number(tn)


def build_ds_tracking_link_from_number(tracking_number: str) -> str:
    """Return the customer-facing Drop Sigma tracking URL for the given
    raw tracking number string.

    Useful from contexts where the Order object isn't loaded (e.g. the
    Order-update Webhook handler that has only the tracking number on
    the request payload). Returns "" if the input is empty/whitespace.
    """
    tn = (tracking_number or "").strip()
    if not tn:
        return ""
    return f"https://{TRACK_DOMAIN}/{tn}/"

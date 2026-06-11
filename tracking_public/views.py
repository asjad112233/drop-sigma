"""Public tracking views — serve track.dropsigma.com.

Two pages + one JSON endpoint:

  GET  /                         → landing (Drop Sigma branded form)
  GET  /<tracking_id>/           → detail (tenant-branded; or "not associated")
  POST /api/lookup/              → AJAX lookup that returns the redirect URL

Strict rule that drives every code path:

  Only tracking numbers that belong to a Drop Sigma TENANT — i.e.
  ``Order.tracking_number`` of an order owned by a Drop Sigma
  ``Store`` whose ``Store.user`` is a real platform tenant — resolve
  to the detail page. Anything else (random carrier number, typo,
  empty string) renders the "Not associated with Drop Sigma" page.
  We never expose which carrier the underlying shipment is on.
"""
from __future__ import annotations

import logging

from django.db.models import Q
from django.http import JsonResponse, Http404
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .stage_mapper import (
    STAGE_DEFS,
    build_stages,
    build_brand_payload,
    normalize_tracking_input,
)

logger = logging.getLogger(__name__)


# ── Landing page ─────────────────────────────────────────────────────
def tracking_landing(request):
    """Drop Sigma branded landing — search form only."""
    return render(request, "tracking_public/landing.html", {
        "page_title": "Track your shipment · Drop Sigma",
    })


# ── Lookup helper (used by both detail view + JSON API) ────────────
def _resolve_order_for_tracking(tracking_input: str):
    """Look up the Order tied to a tracking number.

    Returns ``(order, normalized_input)`` or ``(None, normalized_input)``.

    Strict matching rules:
      1. The tracking number must EXACTLY equal an
         ``Order.tracking_number`` (case-insensitive, whitespace-
         stripped). We never partial-match to avoid accidentally
         leaking a different tenant's shipment.
      2. The matched Order must belong to a Store that is currently
         ``is_active`` and whose ``user`` is set (i.e. a real
         signed-in tenant — never an abandoned / superuser-only row).
    """
    from orders.models import Order
    norm = normalize_tracking_input(tracking_input)
    if not norm:
        return None, norm
    # Case-insensitive exact match on tracking_number. We compare in
    # upper-case so 'yt12345' === 'YT12345' regardless of how WC
    # stored it.
    order = (
        Order.objects
        .filter(tracking_number__iexact=norm)
        .select_related("store", "store__user")
        .filter(store__is_active=True)
        .exclude(store__user__isnull=True)
        .order_by("-id")
        .first()
    )
    return order, norm


# ── Detail page ─────────────────────────────────────────────────────
def tracking_detail(request, tracking_id: str):
    """Render the tenant-branded detail page, or the not-associated
    page if the tracking number isn't owned by a Drop Sigma tenant.
    """
    order, norm = _resolve_order_for_tracking(tracking_id)

    if not order:
        # Strict-rule path: foreign tracking ID. Render not-found.
        return render(request, "tracking_public/not_found.html", {
            "tracking_id": norm or tracking_id,
        }, status=404)

    stages_payload = build_stages(order)
    brand = build_brand_payload(order.store)

    # The displayed tracking number is whatever the customer typed (in
    # normalised form). We never show the carrier name, carrier URL,
    # or hand-off references.
    display_tracking = norm

    # Filter the stages we render in the timeline card — only stages
    # up to (and including) the active one. The 6-stage stepper at
    # the top renders ALL of them with their progress states.
    journey_stages = [
        s for s in stages_payload["stages"]
        if s["status"] in ("done", "active")
    ]

    ctx = {
        "page_title": f"Shipment {display_tracking}",
        "brand": brand,
        "tracking_id": display_tracking,
        "stepper_stages": stages_payload["stages"],
        "active_index":   stages_payload["active_index"],
        "active_key":     stages_payload["active_key"],
        "journey_stages": journey_stages,
        "is_delivered":   stages_payload["is_delivered"],
        "is_failed":      stages_payload["is_failed"],
        "headline":       stages_payload["headline"],
        "headline_sub":   stages_payload["headline_sub"],
        "updated_label":  stages_payload["updated_label"],
        # Tiny meta for the hero — never reveal anything about the
        # underlying order. Just "1 parcel · Express international service".
        "service_label":  "Express international service",
        "parcel_count":   1,
    }
    return render(request, "tracking_public/detail.html", ctx)


# ── JSON lookup API (used by landing-page form) ────────────────────
@csrf_exempt
@require_http_methods(["POST"])
def tracking_lookup_api(request):
    """Resolve a tracking number → redirect URL.

    Frontend posts ``{ "tracking_id": "DS-AU-83619472" }`` (JSON or
    form-encoded). We return ``{ "ok": true, "redirect_url": "/…/" }``
    when the ID belongs to a Drop Sigma tenant, or
    ``{ "ok": false, "reason": "not_associated" }`` otherwise.

    We deliberately don't tell the client WHO the seller is at this
    stage — that info is only revealed on the detail page render, so
    a script can't iterate tracking numbers and dump tenant identity
    from the API alone.
    """
    tracking_input = ""
    try:
        if request.content_type and "application/json" in request.content_type.lower():
            import json
            data = json.loads(request.body or b"{}")
            tracking_input = (data.get("tracking_id") or "").strip()
        else:
            tracking_input = (request.POST.get("tracking_id") or "").strip()
    except Exception:
        tracking_input = ""

    if not tracking_input:
        return JsonResponse({"ok": False, "reason": "empty"}, status=400)

    order, norm = _resolve_order_for_tracking(tracking_input)
    if not order:
        return JsonResponse({"ok": False, "reason": "not_associated"}, status=404)

    return JsonResponse({
        "ok": True,
        "redirect_url": f"/{norm}/",
    })

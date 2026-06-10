"""Seed dense, realistic demo data into the DropSigma Sourcing Partners
workspace tabs (Orders / Catalog / Stock / Payments / Documents / Activity
/ Auto-Rules / Reviews / Chat) on top of the 8 partners created by
``seed_partners``. Idempotent — re-running updates instead of duplicating.

Usage::

    python manage.py seed_workspace            # default --username=admin
    python manage.py seed_workspace --username=alice
"""
from __future__ import annotations

import random
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Tuple

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from sourcing_partners.models import (
    AutoRule,
    LockedPrice,
    PartnerConversation,
    PartnerMessage,
    PartnerReview,
    PriceHistory,
    SourcingPartner,
    VendorDocument,
    VendorOrder,
    VendorPayment,
    VendorStockItem,
    WorkspaceActivity,
)


# ════════════════════════════════════════════════════════════════════════
# Static catalogues — keyed by partner slug so each card has on-brand SKUs
# ════════════════════════════════════════════════════════════════════════

PARTNER_CATALOG: Dict[str, Dict] = {
    "shenzhen-velocity": {
        "sku_prefix": "SV",
        "products": [
            ("EB-PRO-BLK", "Bluetooth Earbuds Pro · Black"),
            ("EB-PRO-WHT", "Bluetooth Earbuds Pro · White"),
            ("PWR-10K", "Magnetic Power Bank 10000mAh"),
            ("SW-FIT-44", "Smartwatch Fit 44mm"),
            ("CAB-USBC", "Braided USB-C Cable 1.5m"),
            ("LMP-RGB", "RGB Desk Lamp · Touch Control"),
            ("HUB-7IN1", "7-in-1 USB-C Hub"),
            ("MIC-CONDR", "Condenser Streaming Mic"),
        ],
    },
    "guangzhou-thread-house": {
        "sku_prefix": "GT",
        "products": [
            ("HD-001", "Premium Heavyweight Hoodie · Charcoal"),
            ("HD-002", "Cropped Hoodie · Cream"),
            ("TEE-BOX", "Boxy Streetwear Tee"),
            ("JNG-001", "Tech-Fleece Joggers"),
            ("CAP-DAD", "Embroidered Dad Cap"),
            ("BAG-TOTE", "Heavyweight Canvas Tote"),
            ("SOX-RIB", "Ribbed Crew Socks 3-pack"),
            ("BOMB-001", "MA-1 Bomber Jacket"),
        ],
    },
    "yiwu-bazaar-direct": {
        "sku_prefix": "YB",
        "products": [
            ("KIT-PEEL", "Silicone Garlic Peeler"),
            ("HM-CLOCK", "Minimalist Wall Clock"),
            ("TOY-FIDG", "Fidget Stress Cube"),
            ("STN-PEN5", "Pastel Gel Pen Set (5)"),
            ("GFT-CAND", "Soy Candle Travel Tin"),
            ("KIT-MAT", "Non-slip Kitchen Mat"),
            ("HM-LIGHT", "LED Strip Light 5m"),
            ("STN-NOTE", "A5 Dotted Notebook"),
        ],
    },
    "ningbo-beauty-supply": {
        "sku_prefix": "NB",
        "products": [
            ("SKN-VITC", "Vitamin-C Brightening Serum 30ml"),
            ("SKN-HA", "Hyaluronic Acid Toner 200ml"),
            ("CSM-LIP", "Velvet Matte Lip Tint"),
            ("HAIR-OIL", "Argan Hair Oil 100ml"),
            ("SKN-CLN", "Gentle Foaming Cleanser"),
            ("CSM-BRSH", "Vegan Makeup Brush Set"),
            ("BTH-SOAP", "Charcoal Detox Bar Soap"),
            ("SKN-MASK", "Sheet Mask Pack of 10"),
        ],
    },
    "dongguan-injection-works": {
        "sku_prefix": "DI",
        "products": [
            ("CASE-IP15", "Custom iPhone 15 Case"),
            ("HOLD-DASH", "Magnetic Dashboard Phone Holder"),
            ("ORG-DRWR", "Modular Drawer Organiser"),
            ("FNL-BOTL", "Travel Funnel Bottle Set"),
            ("STND-LAP", "Aluminium Laptop Stand"),
            ("CASE-WTC", "Smartwatch Bumper Case"),
            ("MOLD-001", "Custom Silicone Mould Kit"),
            ("CLIP-CAB", "Cable Management Clips x12"),
        ],
    },
    "xiamen-home-decor": {
        "sku_prefix": "XH",
        "products": [
            ("VAS-CERM", "Sculpted Ceramic Vase"),
            ("CSH-LINEN", "Linen Cushion Cover 45cm"),
            ("LMP-MUSH", "Mushroom Bedside Lamp"),
            ("PLT-FAUX", "Faux Monstera Plant"),
            ("MIR-ARCH", "Arched Wall Mirror"),
            ("RUG-TUFT", "Hand-Tufted Accent Rug"),
            ("DSH-CRMC", "Stoneware Pasta Bowl Set"),
            ("CANDL-PIL", "Twisted Pillar Candle"),
        ],
    },
    "shanghai-techlogix": {
        "sku_prefix": "ST",
        "products": [
            ("DRN-MINI", "Pocket Selfie Drone 4K"),
            ("PROJ-MINI", "Mini HD Projector"),
            ("VR-LITE", "VR Headset Lite"),
            ("SPK-360", "360° Bluetooth Speaker"),
            ("HEAD-ANC", "ANC Over-Ear Headphones"),
            ("CAM-DASH", "Dash Cam 1080p WiFi"),
            ("ROBO-VAC", "Robot Vacuum Slim"),
            ("KB-MECH", "Wireless Mechanical Keyboard"),
        ],
    },
    "istanbul-style-co": {
        "sku_prefix": "IS",
        "products": [
            ("DNM-SLIM", "Slim-Fit Selvedge Jeans"),
            ("KNT-MERN", "Merino Knit Sweater"),
            ("OUT-WOOL", "Wool Topcoat Camel"),
            ("DNM-JKT", "Denim Trucker Jacket"),
            ("KNT-CARD", "Chunky Cardigan"),
            ("DNM-MOM", "High-Rise Mom Jeans"),
            ("SHRT-OXF", "Oxford Button-Down"),
            ("KNT-POLO", "Knitted Polo Shirt"),
        ],
    },
}

CUSTOMERS = [
    ("Olivia Bennett", "olivia.bennett@example.com",  "Brooklyn",      "USA",       "245 Bedford Ave, Brooklyn NY 11211, USA"),
    ("Marcus Davis",   "marcus.davis@example.com",    "Austin",        "USA",       "1100 Congress Ave, Austin TX 78701, USA"),
    ("Sophia Reynolds","sophia.reynolds@example.com", "Los Angeles",   "USA",       "8800 Sunset Blvd, West Hollywood CA 90069, USA"),
    ("Liam Thompson",  "liam.thompson@example.com",   "Manchester",    "UK",        "12 Oxford Rd, Manchester M1 5QA, United Kingdom"),
    ("Charlotte Hayes","charlotte.hayes@example.com", "London",        "UK",        "57 Brick Lane, London E1 6QL, United Kingdom"),
    ("Ethan Walker",   "ethan.walker@example.com",    "Edinburgh",     "UK",        "9 Princes St, Edinburgh EH2 2EQ, United Kingdom"),
    ("Amelia Cooper",  "amelia.cooper@example.com",   "Sydney",        "Australia", "75 Crown St, Surry Hills NSW 2010, Australia"),
    ("Noah Mitchell",  "noah.mitchell@example.com",   "Melbourne",     "Australia", "212 Brunswick St, Fitzroy VIC 3065, Australia"),
    ("Isabella Carter","isabella.carter@example.com", "Toronto",       "Canada",    "401 Queen St W, Toronto ON M5V 2A8, Canada"),
    ("Jackson Reed",   "jackson.reed@example.com",    "Vancouver",     "Canada",    "1055 Robson St, Vancouver BC V6E 1A9, Canada"),
    ("Mia Hughes",     "mia.hughes@example.com",      "Auckland",      "New Zealand","18 Ponsonby Rd, Auckland 1011, New Zealand"),
    ("Lucas Foster",   "lucas.foster@example.com",    "Chicago",       "USA",       "401 N Wabash Ave, Chicago IL 60611, USA"),
    ("Ava Richardson", "ava.richardson@example.com",  "Miami",         "USA",       "1100 Lincoln Rd, Miami Beach FL 33139, USA"),
    ("Henry Brooks",   "henry.brooks@example.com",    "Birmingham",    "UK",        "215 Broad St, Birmingham B1 2JF, United Kingdom"),
    ("Grace Coleman",  "grace.coleman@example.com",   "Brisbane",      "Australia", "150 Edward St, Brisbane QLD 4000, Australia"),
]

ORDER_STATUS_MIX = [
    "awaiting_quote", "awaiting_quote",
    "quote_received", "quote_received",
    "quote_rejected",
    "paid", "paid",
    "ordered_china",
    "in_transit",
    "at_warehouse",
    "shipped",
    "delivered", "delivered",
]
# Roughly 12 orders per partner; we may add a stray cancelled/disputed below.

CARRIERS = [
    ("DHL",   "https://track.dhl.com/?tracking-id={t}"),
    ("FedEx", "https://www.fedex.com/fedextrack/?trknbr={t}"),
    ("UPS",   "https://www.ups.com/track?tracknum={t}"),
]

# Status lifecycle order — used to back-fill timestamps consistently.
LIFECYCLE_ORDER = [
    "awaiting_quote", "quote_received", "quote_rejected",
    "paid", "ordered_china", "in_transit", "at_warehouse",
    "shipped", "delivered", "cancelled", "disputed",
]


# ════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════

def _q(n) -> Decimal:
    return Decimal(n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _ago(days: int, hours: int = 0, minutes: int = 0):
    return timezone.now() - timedelta(days=days, hours=hours, minutes=minutes)


def _gen_china_tracking() -> str:
    return f"SF{random.randint(10**9, 10**10 - 1)}CN"


def _gen_last_mile_tracking() -> str:
    return f"1Z999AA10{random.randint(100000000, 999999999)}"


def _make_money(china_cny: Decimal, lead_days: int) -> Dict[str, Decimal]:
    """Compute a realistic price breakdown for an order/locked price."""
    china_cny = _q(china_cny)
    china_usd = _q(china_cny * Decimal("0.14"))
    shipping  = _q(Decimal(random.uniform(1.0, 5.0)))
    # Vendor margin: 12-28 % of china_usd, but at least $4.
    margin_pct = Decimal(random.uniform(0.12, 0.28))
    vendor_margin = _q(max(china_usd * margin_pct, Decimal("4.00")))
    subtotal = china_usd + shipping + vendor_margin
    platform_fee = _q(subtotal * Decimal("0.03"))
    tenant_total = _q(subtotal + platform_fee)
    return {
        "china_cost_cny":   china_cny,
        "china_cost_usd":   china_usd,
        "shipping_usd":     shipping,
        "vendor_margin_usd": vendor_margin,
        "platform_fee_usd": platform_fee,
        "tenant_total_usd": tenant_total,
        "lead_days":        lead_days,
    }


# ════════════════════════════════════════════════════════════════════════
# Per-partner seeding
# ════════════════════════════════════════════════════════════════════════

class _PartnerSeeder:
    """Encapsulates the per-partner seeding flow so we can track counts."""

    def __init__(self, tenant, partner, order_ref_counter: List[int]):
        self.tenant = tenant
        self.partner = partner
        self.catalog = PARTNER_CATALOG.get(partner.slug)
        if not self.catalog:
            raise ValueError(f"No catalog for partner slug {partner.slug!r}")
        self.sku_prefix = self.catalog["sku_prefix"]
        self.products = self.catalog["products"]
        self.order_ref_counter = order_ref_counter  # shared mutable

        self.counts: Dict[str, int] = defaultdict(int)
        self.orders: List[VendorOrder] = []
        self.locked_by_sku: Dict[str, LockedPrice] = {}

    # ----- order_ref allocator ------------------------------------------------
    def _next_ref(self) -> str:
        ref = f"DSV-{self.order_ref_counter[0]:04d}"
        self.order_ref_counter[0] += 1
        return ref

    # ----- product helpers ----------------------------------------------------
    def _full_sku(self, suffix: str) -> str:
        return f"{self.sku_prefix}-{suffix}"

    def _product(self, idx: int) -> Tuple[str, str]:
        suffix, name = self.products[idx % len(self.products)]
        return self._full_sku(suffix), name

    # ----- ORDERS -------------------------------------------------------------
    def seed_orders(self):
        statuses = list(ORDER_STATUS_MIX)
        # Occasionally toss in a cancelled or disputed for variety.
        if random.random() < 0.5:
            statuses.append("cancelled")
        if random.random() < 0.3:
            statuses.append("disputed")
        random.shuffle(statuses)

        for i, status in enumerate(statuses):
            product_idx = i  # walks through partner's SKUs
            sku, product_name = self._product(product_idx)
            customer = random.choice(CUSTOMERS)
            qty = random.choice([1, 1, 1, 2, 2, 3])
            cny = Decimal(random.randint(50, 1500))
            money = _make_money(cny, lead_days=self.partner.typical_lead_days)

            created_at = _ago(random.randint(1, 30),
                              hours=random.randint(0, 23),
                              minutes=random.randint(0, 59))
            lifecycle = self._lifecycle_timestamps(status, created_at)

            ref = None
            existing = VendorOrder.objects.filter(
                tenant=self.tenant, partner=self.partner,
                product_sku=sku, customer_email=customer[1],
            ).order_by("created_at").first()
            if existing:
                ref = existing.order_ref
            else:
                ref = self._next_ref()

            defaults = {
                "tenant":             self.tenant,
                "partner":            self.partner,
                "source_order_ref":   f"SHOP-{random.randint(10000, 99999)}",
                "product_sku":        sku,
                "product_name":       product_name,
                "product_image_url":  f"https://picsum.photos/seed/{sku}/200/200",
                "variant":            random.choice(["", "Size M", "Size L",
                                                     "Black", "White",
                                                     "Pack of 2", ""]),
                "quantity":           qty,
                "customer_name":      customer[0],
                "customer_email":     customer[1],
                "ship_city":          customer[2],
                "ship_country":       customer[3],
                "ship_full_address":  customer[4],
                "china_cost_cny":     money["china_cost_cny"],
                "china_cost_usd":     money["china_cost_usd"],
                "shipping_usd":       money["shipping_usd"],
                "vendor_margin_usd":  money["vendor_margin_usd"],
                "platform_fee_usd":   money["platform_fee_usd"],
                "tenant_total_usd":   money["tenant_total_usd"],
                "status":             status,
                "lead_days_quoted":   self.partner.typical_lead_days,
                "created_at":         created_at,
            }
            defaults.update(lifecycle)

            # Tracking on shipped/delivered.
            if status in {"shipped", "delivered"}:
                carrier, url_tmpl = random.choice(CARRIERS)
                last_mile = _gen_last_mile_tracking()
                defaults.update({
                    "china_tracking_no":  _gen_china_tracking(),
                    "last_mile_carrier":  carrier,
                    "last_mile_tracking": last_mile,
                    "last_mile_url":      url_tmpl.format(t=last_mile),
                })

            order, was_created = VendorOrder.objects.update_or_create(
                order_ref=ref,
                defaults=defaults,
            )
            self.orders.append(order)
            self.counts["orders_created" if was_created else "orders_updated"] += 1

    @staticmethod
    def _lifecycle_timestamps(status: str, created_at) -> Dict:
        """Back-fill timestamps so the order's history is internally consistent."""
        if status not in LIFECYCLE_ORDER:
            return {}
        fields = {
            "awaiting_quote":  [],
            "quote_received":  ["quoted_at"],
            "quote_rejected":  ["quoted_at"],
            "paid":            ["quoted_at", "paid_at"],
            "ordered_china":   ["quoted_at", "paid_at", "ordered_at"],
            "in_transit":      ["quoted_at", "paid_at", "ordered_at", "in_transit_at"],
            "at_warehouse":    ["quoted_at", "paid_at", "ordered_at", "in_transit_at", "received_at"],
            "shipped":         ["quoted_at", "paid_at", "ordered_at", "in_transit_at", "received_at", "shipped_at"],
            "delivered":       ["quoted_at", "paid_at", "ordered_at", "in_transit_at", "received_at", "shipped_at", "delivered_at"],
            "cancelled":       ["quoted_at", "cancelled_at"],
            "disputed":        ["quoted_at", "paid_at"],
        }[status]
        out = {}
        ts = created_at
        for f in fields:
            ts = ts + timedelta(hours=random.randint(6, 48))
            if ts > timezone.now():
                ts = timezone.now() - timedelta(hours=random.randint(1, 12))
            out[f] = ts
        return out

    # ----- LOCKED PRICES + HISTORY -------------------------------------------
    def seed_locked_prices(self):
        # Eligible orders: anything that hit "paid" or later.
        paid_orders = [o for o in self.orders if o.status in {
            "paid", "ordered_china", "in_transit", "at_warehouse",
            "shipped", "delivered",
        }]
        by_sku = defaultdict(list)
        for o in paid_orders:
            by_sku[o.product_sku].append(o)

        # Decide which sku gets renegotiation_proposed (one per partner).
        renegot_sku = next(iter(by_sku.keys()), None) if by_sku else None

        for sku, orders in by_sku.items():
            orders.sort(key=lambda o: o.created_at)
            first = orders[0]
            latest = orders[-1]
            lp_status = "renegotiation_proposed" if sku == renegot_sku else "active"

            defaults = {
                "product_name":         first.product_name,
                "product_image_url":    first.product_image_url,
                "china_cost_cny":       first.china_cost_cny,
                "china_cost_usd":       first.china_cost_usd,
                "shipping_usd":         first.shipping_usd,
                "vendor_margin_usd":    first.vendor_margin_usd,
                "platform_fee_usd":     first.platform_fee_usd,
                "locked_price_usd":     first.tenant_total_usd,
                "lead_days":            first.lead_days_quoted or self.partner.typical_lead_days,
                "auto_assign":          True,
                "status":               lp_status,
                "locked_via_order_ref": first.order_ref,
                "locked_at":            first.paid_at or first.created_at,
                "last_used_at":         latest.paid_at or latest.created_at,
                "times_used":           len(orders),
            }
            if lp_status == "renegotiation_proposed":
                defaults["pending_price_usd"] = _q(first.tenant_total_usd * Decimal("1.10"))
                defaults["pending_reason"] = (
                    "Supplier raised CNY cost ~8%, requesting price update."
                )
                defaults["pending_proposed_at"] = _ago(random.randint(1, 7))

            lp, was_created = LockedPrice.objects.update_or_create(
                tenant=self.tenant, partner=self.partner, sku=sku,
                defaults=defaults,
            )
            self.locked_by_sku[sku] = lp
            self.counts["locked_created" if was_created else "locked_updated"] += 1

            # Link the first order to this locked price.
            if first.locked_price_used_id != lp.id:
                first.locked_price_used = lp
                first.save(update_fields=["locked_price_used"])

            # ----- PRICE HISTORY (replace + recreate to stay idempotent) ---
            PriceHistory.objects.filter(locked=lp).delete()
            ts0 = lp.locked_at
            PriceHistory.objects.create(
                locked=lp, event="locked",
                old_price=None,
                new_price=lp.locked_price_usd,
                reason=f"Locked via {lp.locked_via_order_ref}",
                acted_by="tenant",
            )
            self.counts["price_history_created"] += 1
            if lp_status == "renegotiation_proposed":
                ph = PriceHistory.objects.create(
                    locked=lp, event="renegotiation_proposed",
                    old_price=lp.locked_price_usd,
                    new_price=lp.pending_price_usd,
                    reason=lp.pending_reason,
                    acted_by="vendor",
                )
                self.counts["price_history_created"] += 1
            elif random.random() < 0.4:
                # Some locked SKUs have a successful past renegotiation.
                old = _q(lp.locked_price_usd / Decimal("1.05"))
                PriceHistory.objects.create(
                    locked=lp, event="renegotiation_approved",
                    old_price=old,
                    new_price=lp.locked_price_usd,
                    reason="Q2 raw-material adjustment, approved.",
                    acted_by="tenant",
                )
                self.counts["price_history_created"] += 1

    # ----- STOCK ITEMS --------------------------------------------------------
    def seed_stock(self):
        # Use SKUs the tenant orders frequently (the ones we've made locked
        # prices for, or the partner's first few SKUs as a fallback).
        sku_pool = list(self.locked_by_sku.keys())
        if len(sku_pool) < 4:
            for sfx, _name in self.products:
                full = self._full_sku(sfx)
                if full not in sku_pool:
                    sku_pool.append(full)
                if len(sku_pool) >= 6:
                    break

        target = random.randint(4, min(8, len(sku_pool)))
        chosen = sku_pool[:target]

        # Decide which slots get which status.
        statuses = ["out", "critical", "low", "low", "good", "good", "good", "good"][:target]
        random.shuffle(statuses)

        product_lookup = {self._full_sku(s): n for s, n in self.products}

        for sku, slot in zip(chosen, statuses):
            low_threshold = random.choice([10, 15, 20, 25])
            if slot == "out":
                qty_on_hand = 0
            elif slot == "critical":
                qty_on_hand = random.randint(1, 2)
            elif slot == "low":
                qty_on_hand = random.randint(3, low_threshold)
            else:
                qty_on_hand = random.randint(low_threshold + 5, low_threshold * 4)

            qty_inbound = random.choice([0, 0, 0, 25, 50, 100])
            qty_reserved = random.randint(0, 3)
            name = product_lookup.get(sku, sku.replace("-", " ").title())
            defaults = {
                "product_name": name,
                "image_url":    f"https://picsum.photos/seed/{sku}/200/200",
                "qty_on_hand":  qty_on_hand,
                "qty_reserved": qty_reserved,
                "qty_inbound":  qty_inbound,
                "low_threshold": low_threshold,
            }
            _, was_created = VendorStockItem.objects.update_or_create(
                tenant=self.tenant, partner=self.partner, sku=sku,
                defaults=defaults,
            )
            self.counts["stock_created" if was_created else "stock_updated"] += 1

    # ----- PAYMENTS -----------------------------------------------------------
    def seed_payments(self):
        # One settled order-payment per paid+ order.
        paid_states = {"paid", "ordered_china", "in_transit",
                       "at_warehouse", "shipped", "delivered"}
        cancelled_orders = [o for o in self.orders if o.status == "cancelled"]

        # Wipe order-linked payments to keep this idempotent without dupes.
        VendorPayment.objects.filter(
            tenant=self.tenant, partner=self.partner,
        ).delete()

        for o in self.orders:
            if o.status not in paid_states:
                continue
            VendorPayment.objects.create(
                tenant=self.tenant, partner=self.partner,
                related_order=o,
                payment_type="order",
                method=random.choice(["wallet", "stripe", "wise"]),
                amount_usd=o.tenant_total_usd,
                status="settled",
                reference=o.order_ref,
                note=f"Payment for {o.product_name}",
                settled_at=o.paid_at or o.created_at,
            )
            self.counts["payments_created"] += 1

        # Sample request payment.
        VendorPayment.objects.create(
            tenant=self.tenant, partner=self.partner,
            payment_type="sample",
            method="wallet",
            amount_usd=_q(Decimal(random.uniform(15, 30))),
            status="settled",
            reference=f"SMPL-{random.randint(1000, 9999)}",
            note="Sample request for new SKU evaluation",
            settled_at=_ago(random.randint(5, 25)),
        )
        self.counts["payments_created"] += 1

        # Refund for a cancelled order, if any.
        if cancelled_orders:
            target = cancelled_orders[0]
            VendorPayment.objects.create(
                tenant=self.tenant, partner=self.partner,
                related_order=target,
                payment_type="refund",
                method=random.choice(["wallet", "stripe"]),
                amount_usd=target.tenant_total_usd,
                status="refunded",
                reference=f"RFD-{target.order_ref}",
                note=f"Refund for cancelled order {target.order_ref}",
                settled_at=target.cancelled_at or _ago(random.randint(1, 10)),
            )
            self.counts["payments_created"] += 1

    # ----- DOCUMENTS ----------------------------------------------------------
    def seed_documents(self):
        templates = [
            ("spec",        "Tech Pack v3 — {name}",        "tenant"),
            ("spec",        "Material spec — {name}",       "vendor"),
            ("sample",      "Sample Photos · {name}",       "vendor"),
            ("sample",      "Pre-production sample · {name}", "vendor"),
            ("contract",    "Service Agreement signed 03 Jun 2026", "tenant"),
            ("invoice",     "Invoice INV-2026-05 (May)",    "vendor"),
            ("shipping",    "Shipping label template",      "vendor"),
            ("certificate", random.choice([
                                "CE certification",
                                "FDA registration",
                                "ISO 9001 certificate",
                                "RoHS compliance"
                            ]),                              "vendor"),
            ("other",       "Vendor onboarding checklist",  "tenant"),
        ]

        # Clear and reseed — easier than match-by-title.
        VendorDocument.objects.filter(
            tenant=self.tenant, partner=self.partner,
        ).delete()

        for cat, title_tpl, who in templates:
            sku, name = self._product(random.randint(0, len(self.products) - 1))
            title = title_tpl.format(name=name)
            doc = VendorDocument.objects.create(
                tenant=self.tenant, partner=self.partner,
                category=cat,
                title=title,
                external_url=f"https://picsum.photos/seed/{sku}-{cat}/600/400",
                filename=f"{title.lower().replace(' ', '_')[:40]}.pdf",
                filesize_bytes=random.randint(100_000, 5_000_000),
                description=f"{cat.title()} document for {self.partner.name}.",
                uploaded_by=who,
            )
            # Backdate created_at since auto_now_add fires on .create().
            VendorDocument.objects.filter(pk=doc.pk).update(
                created_at=_ago(random.randint(1, 90)),
            )
            self.counts["documents_created"] += 1

    # ----- AUTO RULES ---------------------------------------------------------
    def seed_auto_rules(self, disable_all: bool = False):
        rule_specs = [
            ("auto_route",     {"category": random.choice(self.partner.specialties or ["Apparel"])}),
            ("auto_pay",       {"max_amount_usd": random.choice([50, 75, 100, 150])}),
        ]
        if random.random() < 0.6:
            rule_specs.append(("auto_reorder",
                               {"days_supply": random.choice([7, 14, 21])}))
        if random.random() < 0.5:
            rule_specs.append(("quote_timeout",
                               {"hours": random.choice([24, 48, 72])}))

        # Idempotent: replace existing rules per (tenant, partner, rule_type).
        for rule_type, cfg in rule_specs:
            _, was_created = AutoRule.objects.update_or_create(
                tenant=self.tenant, partner=self.partner, rule_type=rule_type,
                defaults={
                    "config":    cfg,
                    "is_active": False if disable_all else True,
                },
            )
            self.counts["rules_created" if was_created else "rules_updated"] += 1

    # ----- REVIEWS ------------------------------------------------------------
    def seed_reviews(self):
        delivered = [o for o in self.orders if o.status == "delivered"]
        target = min(len(delivered), random.randint(3, 5))
        # Some delivered orders may exist; we may need a few more
        # (the mix only guarantees ~2). Use whatever's available.
        target = max(target, min(2, len(delivered)))

        # Clear existing reviews to keep deterministic.
        PartnerReview.objects.filter(
            tenant=self.tenant, partner=self.partner,
        ).delete()

        comments = [
            "Excellent quality, on-time delivery, will reorder.",
            "Quick to respond and pricing came in below what we expected.",
            "Packaging was clean, customer feedback has been overwhelmingly positive.",
            "Solid partner — communication in English was clear throughout.",
            "Great QC photos before shipping, no defects on arrival.",
        ]
        used = delivered[:target]
        for o in used:
            PartnerReview.objects.create(
                tenant=self.tenant, partner=self.partner,
                related_order=o,
                rating=random.choice([4, 5, 5, 5]),
                comm_score=random.choice([4, 5, 5]),
                quality_score=random.choice([4, 5, 5]),
                delivery_score=random.choice([4, 5, 5]),
                comment=random.choice(comments),
                is_public=random.random() < 0.85,
            )
            self.counts["reviews_created"] += 1

        # Recompute partner rating once at the end.
        return target  # consumed for end-of-run summary

    # ----- CONVERSATIONS ------------------------------------------------------
    def seed_conversation(self, force_unread: bool, dense_thread: bool):
        convo, was_created = PartnerConversation.objects.update_or_create(
            tenant=self.tenant, partner=self.partner,
            defaults={"is_archived": False},
        )
        self.counts["convos_created" if was_created else "convos_updated"] += 1

        if was_created or convo.messages.count() == 0:
            PartnerMessage.objects.create(
                conversation=convo,
                direction="in",
                body=(f"Hi! Thanks for connecting with {self.partner.name}. "
                      "Send any RFQ here and we'll quote within "
                      f"{self.partner.avg_response_hours}h."),
                is_read=True,
            )
            self.counts["messages_created"] += 1

        if dense_thread and convo.messages.count() <= 1:
            scripted = [
                ("out", "Hey team, can you quote SKU "
                        f"{self._product(0)[0]} at qty 200?"),
                ("in",  "Sure — give us 24 h, will reply with FOB + DDP."),
                ("out", "Also need updated MOQ for the new variant."),
                ("in",  "MOQ stays at 100. We can do mixed colours at 50/colour."),
                ("out", "Perfect, please send a photo of the latest sample."),
                ("in",  "Sending now — pls confirm the wash label colour."),
            ]
            for direction, body in scripted:
                PartnerMessage.objects.create(
                    conversation=convo, direction=direction,
                    body=body, is_read=(direction == "out"),
                )
                self.counts["messages_created"] += 1

        # Refresh preview/last_message_at to the most recent message.
        last = convo.messages.order_by("-created_at").first()
        if last:
            convo.last_message_preview = last.body[:200]
            convo.last_message_at = last.created_at

        convo.unread_count_for_tenant = (
            random.randint(1, 4) if force_unread else 0
        )
        convo.save(update_fields=[
            "last_message_preview", "last_message_at",
            "unread_count_for_tenant",
        ])

    # ----- ACTIVITY -----------------------------------------------------------
    def seed_activity(self):
        WorkspaceActivity.objects.filter(
            tenant=self.tenant, partner=self.partner,
        ).delete()

        entries = []
        for o in self.orders:
            if o.status == "quote_received":
                entries.append(dict(
                    when=o.quoted_at or o.created_at,
                    event_type="quote.received",
                    title=f"Quote received for {o.order_ref}",
                    detail=f"${o.tenant_total_usd} for {o.product_name}",
                    icon="💰", actor="vendor", related_order=o,
                ))
            if o.status in {"paid", "ordered_china", "in_transit",
                            "at_warehouse", "shipped", "delivered"}:
                entries.append(dict(
                    when=o.paid_at or o.created_at,
                    event_type="order.paid",
                    title=f"Paid {o.order_ref}",
                    detail=f"${o.tenant_total_usd} via wallet",
                    icon="💳", actor="tenant", related_order=o,
                ))
            if o.status in {"shipped", "delivered"}:
                entries.append(dict(
                    when=o.shipped_at or o.created_at,
                    event_type="order.shipped",
                    title=f"{o.order_ref} shipped to customer",
                    detail=f"{o.last_mile_carrier} · {o.last_mile_tracking}",
                    icon="🚚", actor="vendor", related_order=o,
                ))
            if o.status == "delivered":
                entries.append(dict(
                    when=o.delivered_at or o.created_at,
                    event_type="order.delivered",
                    title=f"{o.order_ref} delivered",
                    detail=f"To {o.ship_city}, {o.ship_country}",
                    icon="✅", actor="system", related_order=o,
                ))
            if o.status == "in_transit":
                entries.append(dict(
                    when=o.in_transit_at or o.created_at,
                    event_type="order.in_transit",
                    title=f"{o.order_ref} en route to warehouse",
                    detail=f"Tracking {o.china_tracking_no or 'pending'}",
                    icon="✈️", actor="vendor", related_order=o,
                ))

        for sku, lp in self.locked_by_sku.items():
            entries.append(dict(
                when=lp.locked_at,
                event_type="lockedprice.created",
                title=f"Price locked for {sku}",
                detail=f"${lp.locked_price_usd} per unit",
                icon="🔒", actor="tenant",
            ))
            if lp.status == "renegotiation_proposed":
                entries.append(dict(
                    when=lp.pending_proposed_at or _ago(2),
                    event_type="lockedprice.renegotiation_proposed",
                    title=f"Vendor proposed new price for {sku}",
                    detail=lp.pending_reason,
                    icon="⚠️", actor="vendor",
                ))

        # Stock reorder events.
        for stock in VendorStockItem.objects.filter(
            tenant=self.tenant, partner=self.partner, qty_inbound__gt=0,
        ):
            entries.append(dict(
                when=_ago(random.randint(1, 14)),
                event_type="stock.reorder_requested",
                title=f"Reorder placed for {stock.sku}",
                detail=f"{stock.qty_inbound} units inbound",
                icon="📦", actor="tenant",
            ))

        # Sprinkle: payment.made + document.uploaded + review.submitted.
        for _ in range(random.randint(2, 4)):
            entries.append(dict(
                when=_ago(random.randint(1, 28)),
                event_type="payment.made",
                title="Wallet payment recorded",
                detail=f"${random.randint(20, 250)}.00 settled",
                icon="💸", actor="tenant",
            ))
        for _ in range(random.randint(2, 4)):
            entries.append(dict(
                when=_ago(random.randint(1, 60)),
                event_type="document.uploaded",
                title="Document uploaded",
                detail="Spec sheet shared with partner",
                icon="📄", actor=random.choice(["tenant", "vendor"]),
            ))
        for r in PartnerReview.objects.filter(
            tenant=self.tenant, partner=self.partner,
        ):
            entries.append(dict(
                when=r.created_at or _ago(random.randint(1, 14)),
                event_type="review.submitted",
                title=f"Review submitted ({r.rating}★)",
                detail=r.comment[:120],
                icon="⭐", actor="tenant",
                related_order=r.related_order,
            ))

        # Ensure at least 15 entries — pad with system notes if needed.
        while len(entries) < 15:
            entries.append(dict(
                when=_ago(random.randint(1, 30)),
                event_type="system.note",
                title="Partner status pinged",
                detail="Routine workspace heartbeat.",
                icon="•", actor="system",
            ))
        # Cap at 25 to stay within the spec.
        entries = entries[:25]
        entries.sort(key=lambda e: e["when"])

        for e in entries:
            wa = WorkspaceActivity.objects.create(
                tenant=self.tenant, partner=self.partner,
                event_type=e["event_type"],
                title=e["title"],
                detail=e["detail"],
                icon=e["icon"],
                actor=e["actor"],
                related_order=e.get("related_order"),
            )
            WorkspaceActivity.objects.filter(pk=wa.pk).update(
                created_at=e["when"],
            )
            self.counts["activity_created"] += 1


# ════════════════════════════════════════════════════════════════════════
# Management command
# ════════════════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        "Populate the DropSigma Sourcing Partners workspace with rich, "
        "realistic demo data (orders, locked prices, stock, payments, "
        "documents, auto-rules, activity, reviews, chat). Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--username", default="admin",
            help="Username of the tenant to seed for (default: admin).",
        )

    def handle(self, *args, **opts):
        User = get_user_model()
        username = opts["username"]
        try:
            tenant = User.objects.get(username=username)
        except User.DoesNotExist:
            self.stderr.write(self.style.ERROR(
                f"✗ User '{username}' not found — run the user/admin "
                "setup first, then re-run this command."
            ))
            return

        partners = list(SourcingPartner.objects.filter(
            is_active=True,
        ).order_by("sort_order"))
        if not partners:
            self.stderr.write(self.style.ERROR(
                "✗ No active SourcingPartners — run "
                "`python manage.py seed_partners` first."
            ))
            return

        self.stdout.write(self.style.NOTICE(
            f"Seeding workspace for tenant '{tenant.username}' "
            f"across {len(partners)} partners..."
        ))

        # Pick which partners get unread-badged chats / dense threads /
        # disabled auto-rules. These choices are stable across runs because
        # we seed from a fixed value.
        rng_top = random.Random(tenant.id * 1009)
        unread_partner_ids = set(rng_top.sample(
            [p.id for p in partners], k=min(2, len(partners)),
        ))
        dense_partner_ids = set(rng_top.sample(
            [p.id for p in partners], k=min(4, len(partners)),
        ))
        disabled_rules_partner_id = rng_top.choice([p.id for p in partners])

        # Order-ref counter is shared across the whole run.
        order_ref_counter = [self._starting_order_ref()]

        grand_counts: Dict[str, int] = defaultdict(int)
        per_partner_report = []

        for partner in partners:
            try:
                random.seed(tenant.id + partner.id)
                seeder = _PartnerSeeder(tenant, partner, order_ref_counter)
                with transaction.atomic():
                    seeder.seed_orders()
                    seeder.seed_locked_prices()
                    seeder.seed_stock()
                    seeder.seed_payments()
                    seeder.seed_documents()
                    seeder.seed_auto_rules(
                        disable_all=(partner.id == disabled_rules_partner_id),
                    )
                    seeder.seed_reviews()
                    seeder.seed_conversation(
                        force_unread=partner.id in unread_partner_ids,
                        dense_thread=partner.id in dense_partner_ids,
                    )
                    seeder.seed_activity()

                    # Recompute review rollup for this partner.
                    self._recompute_partner_rating(partner, tenant)

            except Exception as exc:    # noqa: BLE001
                self.stderr.write(self.style.ERROR(
                    f"✗ {partner.name}: {exc!r}"
                ))
                continue

            for k, v in seeder.counts.items():
                grand_counts[k] += v
            per_partner_report.append((partner.name, dict(seeder.counts)))
            self.stdout.write(self.style.SUCCESS(
                f"  ✓ {partner.name}: " + ", ".join(
                    f"{k}={v}" for k, v in sorted(seeder.counts.items())
                )
            ))

        self.stdout.write("")
        self.stdout.write(self.style.NOTICE("─── TOTAL ───"))
        for k in sorted(grand_counts):
            self.stdout.write(f"  {k:>26}: {grand_counts[k]}")
        self.stdout.write(self.style.SUCCESS(
            "✓ Workspace seeding complete."
        ))

    # --------------------------------------------------------------------
    def _starting_order_ref(self) -> int:
        """Find the next DSV- counter so we don't collide with prior runs."""
        existing = VendorOrder.objects.filter(
            order_ref__startswith="DSV-",
        ).values_list("order_ref", flat=True)
        max_n = 2400  # start at 2401 if none exist
        for ref in existing:
            try:
                n = int(ref.split("-")[1])
                if n > max_n:
                    max_n = n
            except (IndexError, ValueError):
                continue
        return max_n + 1

    def _recompute_partner_rating(self, partner, tenant) -> None:
        """Recompute partner.rating + .total_reviews across ALL reviews
        (every tenant) so the partner card stays accurate."""
        reviews = PartnerReview.objects.filter(partner=partner)
        total = reviews.count()
        if total == 0:
            return
        avg = sum(r.rating for r in reviews) / total
        partner.rating = Decimal(str(round(avg, 2)))
        partner.total_reviews = total
        partner.save(update_fields=["rating", "total_reviews"])

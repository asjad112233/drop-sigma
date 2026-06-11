"""Seed realistic sample orders into orders.Order so the Sourcing > Orders
view has rich data across all 6 status categories.

Usage:
    ./venv/bin/python manage.py seed_sourcing_orders
    ./venv/bin/python manage.py seed_sourcing_orders --user admin
    ./venv/bin/python manage.py seed_sourcing_orders --user admin --count 12

By default it picks the admin user and creates 12 orders per category
(72 total). Re-running is safe — orders carry a `seed_marker` in
raw_data so we skip ones that already exist with the same marker.
"""
import json
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from orders.models import Order
from stores.models import Store


# A library of realistic product templates with size/color/material variants
# so the multi-variant chip display gets a good showcase. Mixed across
# apparel, electronics, beauty, home.
PRODUCT_TEMPLATES = [
    {"name": "Cotton Crew T-Shirt", "sku_prefix": "TSH-CRW", "category": "apparel",
     "image": "https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?w=300",
     "variants": [{"key": "Size", "values": ["S", "M", "L", "XL"]},
                  {"key": "Color", "values": ["Black", "White", "Navy", "Charcoal"]}],
     "price_range": (18, 32)},
    {"name": "Vintage Leather Strap Watch", "sku_prefix": "WTC-LB", "category": "accessories",
     "image": "https://images.unsplash.com/photo-1524805444758-089113d48a6d?w=300",
     "variants": [{"key": "Color", "values": ["Brown", "Black", "Tan"]},
                  {"key": "Case", "values": ["42mm", "38mm"]}],
     "price_range": (45, 95)},
    {"name": "Wireless Bluetooth Earbuds Pro", "sku_prefix": "AUD-BUD", "category": "electronics",
     "image": "https://images.unsplash.com/photo-1606220588913-b3aacb4d2f46?w=300",
     "variants": [{"key": "Color", "values": ["Midnight Black", "Ivory White", "Rose Gold"]}],
     "price_range": (38, 82)},
    {"name": "Premium Yoga Mat", "sku_prefix": "FIT-YGA", "category": "fitness",
     "image": "https://images.unsplash.com/photo-1591291621164-2c6367723315?w=300",
     "variants": [{"key": "Thickness", "values": ["6mm", "8mm", "10mm"]},
                  {"key": "Color", "values": ["Purple", "Teal", "Charcoal", "Coral"]}],
     "price_range": (24, 48)},
    {"name": "Ceramic Pour-Over Coffee Set", "sku_prefix": "HOM-CFE", "category": "home",
     "image": "https://images.unsplash.com/photo-1495474472287-4d71bcdd2085?w=300",
     "variants": [{"key": "Size", "values": ["1-cup", "2-cup", "4-cup"]},
                  {"key": "Color", "values": ["Matte White", "Slate", "Sand"]}],
     "price_range": (30, 65)},
    {"name": "Kayali Vanilla 28 Eau de Parfum", "sku_prefix": "BEA-KYL", "category": "beauty",
     "image": "https://images.unsplash.com/photo-1541643600914-78b084683601?w=300",
     "variants": [{"key": "Size", "values": ["30ml", "50ml", "100ml"]}],
     "price_range": (45, 165)},
    {"name": "Minimalist Canvas Tote Bag", "sku_prefix": "BAG-TOT", "category": "accessories",
     "image": "https://images.unsplash.com/photo-1544816155-12df9643f363?w=300",
     "variants": [{"key": "Color", "values": ["Natural", "Black", "Olive"]}],
     "price_range": (15, 26)},
    {"name": "USB-C 7-in-1 Multi Hub", "sku_prefix": "ELC-HUB", "category": "electronics",
     "image": "https://images.unsplash.com/photo-1625948515291-69613efd103f?w=300",
     "variants": [{"key": "Finish", "values": ["Space Gray", "Silver"]}],
     "price_range": (28, 42)},
    {"name": "Organic Soy Candle Travel Tin", "sku_prefix": "HOM-CAN", "category": "home",
     "image": "https://images.unsplash.com/photo-1602874801006-e26c4ad65b8c?w=300",
     "variants": [{"key": "Scent", "values": ["Lavender", "Eucalyptus", "Sandalwood", "Vanilla"]},
                  {"key": "Size", "values": ["4oz", "8oz"]}],
     "price_range": (12, 28)},
    {"name": "Adjustable Phone Stand", "sku_prefix": "ELC-STD", "category": "electronics",
     "image": "https://images.unsplash.com/photo-1592890288564-76628a30a657?w=300",
     "variants": [{"key": "Color", "values": ["Black", "White", "Silver"]}],
     "price_range": (10, 22)},
    {"name": "Linen Throw Pillow Cover", "sku_prefix": "HOM-PLW", "category": "home",
     "image": "https://images.unsplash.com/photo-1567225557594-88d73e55f2cb?w=300",
     "variants": [{"key": "Size", "values": ['18"x18"', '20"x20"', '22"x22"']},
                  {"key": "Color", "values": ["Beige", "Sage", "Terracotta", "Charcoal"]}],
     "price_range": (16, 32)},
    {"name": "Hydrating Skincare Set 3-Pack", "sku_prefix": "BEA-SKN", "category": "beauty",
     "image": "https://images.unsplash.com/photo-1556228720-195a672e8a03?w=300",
     "variants": [{"key": "Skin Type", "values": ["Dry", "Oily", "Combination", "Sensitive"]}],
     "price_range": (38, 78)},
]

CUSTOMERS = [
    ("Sarah Henderson",     "sarah.h@example.com",     "+1-415-555-0142",
     "742 Pine Street, Apt 3B", "San Francisco", "CA", "94104", "US"),
    ("Mohammed Al-Rashid",  "m.alrashid@example.ae",   "+971-50-1248379",
     "Marina Plaza, Tower 2, Unit 1804", "Dubai", "", "00000", "AE"),
    ("Emma Williams",       "emma.w@example.co.uk",    "+44-7700-900156",
     "12 Camden High Street", "London", "", "NW1 0JH", "GB"),
    ("Priya Sharma",        "priya.sharma@example.in", "+91-98765-43210",
     "B-204, Sunrise Apartments, Powai", "Mumbai", "MH", "400076", "IN"),
    ("Liam O'Brien",        "liam.ob@example.ie",      "+353-87-1234567",
     "23 Grafton Street", "Dublin", "", "D02 X285", "IE"),
    ("Yuki Tanaka",         "yuki.t@example.jp",       "+81-90-1234-5678",
     "3-15-2 Shibuya", "Tokyo", "", "150-0002", "JP"),
    ("Carlos Mendez",       "c.mendez@example.mx",     "+52-55-1234-5678",
     "Av. Reforma 222, Piso 8", "Mexico City", "CDMX", "06600", "MX"),
    ("Anna Schmidt",        "anna.s@example.de",       "+49-30-12345678",
     "Kantstraße 45", "Berlin", "", "10623", "DE"),
    ("Sophie Dubois",       "sophie.d@example.fr",     "+33-6-12-34-56-78",
     "12 Rue de Rivoli, App 7", "Paris", "", "75001", "FR"),
    ("James Kim",           "james.k@example.kr",      "+82-10-1234-5678",
     "456 Gangnam-daero", "Seoul", "", "06000", "KR"),
    ("Fatima Al-Qatami",    "fatima.q@example.sa",     "+966-50-987-6543",
     "King Fahd Road, Olaya", "Riyadh", "", "12241", "SA"),
    ("Diego Fernandez",     "diego.f@example.es",      "+34-666-123-456",
     "Calle Gran Vía 28", "Madrid", "", "28013", "ES"),
    ("Aisha Patel",         "aisha.p@example.in",      "+91-98201-12345",
     "12 Andheri West", "Mumbai", "MH", "400058", "IN"),
    ("Lucas Costa",         "lucas.c@example.br",      "+55-11-91234-5678",
     "Av. Paulista 1578, Sala 320", "São Paulo", "SP", "01310-200", "BR"),
    ("Maria Garcia",        "m.garcia@example.us",     "+1-305-555-0188",
     "1200 Brickell Ave, Suite 1500", "Miami", "FL", "33131", "US"),
    ("Olu Adebayo",         "olu.a@example.ng",        "+234-803-123-4567",
     "12 Adeola Odeku Street, Victoria Island", "Lagos", "", "101241", "NG"),
    ("Thomas Müller",       "t.mueller@example.de",    "+49-40-87654321",
     "Reeperbahn 78", "Hamburg", "", "20359", "DE"),
    ("Nina Petrova",        "nina.p@example.ru",       "+7-495-123-4567",
     "Tverskaya 15, кв. 42", "Moscow", "", "125009", "RU"),
]

TRACKING_CARRIERS = [
    ("DHL Express",   "https://www.dhl.com/global-en/home/tracking.html?tracking-id={}"),
    ("FedEx",         "https://www.fedex.com/wtrk/track/?trknbr={}"),
    ("UPS",           "https://www.ups.com/track?tracknum={}"),
    ("Aramex",        "https://www.aramex.com/track/results?ShipmentNumber={}"),
]

CATEGORIES = [
    "pending_source", "pending_payment", "processing",
    "shipping",       "delivered",       "cancel",
]


def _build_line_item(rng, template, qty):
    """Return a base line item dict. The `_variant_pairs` field is internal —
    `_build_raw_data` consumes it and shapes the platform-specific variant
    encoding so we don't double-emit (variant_title + properties + meta_data)."""
    sku = f"{template['sku_prefix']}-{rng.randint(100,999)}"
    pairs = []
    for v in template["variants"]:
        choice = rng.choice(v["values"])
        pairs.append((v["key"], choice))

    price = round(rng.uniform(*template["price_range"]), 2)
    return {
        "id":            rng.randint(10**12, 10**13 - 1),
        "name":          template["name"],
        "title":         template["name"],
        "product_name":  template["name"],
        "sku":           sku,
        "quantity":      qty,
        "price":         f"{price:.2f}",
        "total":         f"{price * qty:.2f}",
        "image":         {"src": template["image"]},
        "_variant_pairs": pairs,
    }


def _build_raw_data(rng, platform, customer, line_items, total):
    """Construct a raw_data payload that the Order property parsers can read.

    Shopify uses 'shipping_address' (camelCase address1/2);
    WooCommerce uses 'shipping' (snake_case address_1/2). We mirror the
    platform shape so address parsing in Order._address_from_raw works.
    """
    name, email, phone, line1, city, state, postal, country = customer
    if platform == "shopify":
        address = {
            "name":         name,
            "phone":        phone,
            "address1":     line1,
            "address2":     "",
            "city":         city,
            "province":     state,
            "zip":          postal,
            "country":      country,
            "country_code": country,
        }
        # Shopify: variant_title only (e.g. "Tan / 42mm").
        shopify_items = []
        for li in line_items:
            pairs = li.pop("_variant_pairs", [])
            shopify_items.append({
                **li,
                "variant_title": " / ".join(v for _, v in pairs)
                                  if pairs else "Default Title",
            })
        return {
            "shipping_address": address,
            "billing_address":  address,
            "line_items":       shopify_items,
            "currency":         "USD",
            "total_price":      f"{total:.2f}",
            "email":            email,
            "customer": {"first_name": name.split(" ")[0],
                         "last_name": " ".join(name.split(" ")[1:]),
                         "email": email, "phone": phone},
        }
    # WooCommerce: variants live in meta_data (display_key / display_value).
    first, *rest = name.split(" ")
    last = " ".join(rest)
    address = {
        "first_name":  first,
        "last_name":   last,
        "phone":       phone,
        "address_1":   line1,
        "address_2":   "",
        "city":        city,
        "state":       state,
        "postcode":    postal,
        "country":     country,
        "email":       email,
    }
    wc_items = []
    for li in line_items:
        pairs = li.pop("_variant_pairs", [])
        wc_items.append({
            **li,
            "meta_data": [{"display_key": k, "display_value": v} for k, v in pairs],
        })
    return {
        "shipping": address,
        "billing":  address,
        "line_items": wc_items,
        "currency": "USD",
        "total":    f"{total:.2f}",
    }


class Command(BaseCommand):
    help = "Seed realistic sample orders across all 6 sourcing categories."

    def add_arguments(self, parser):
        parser.add_argument("--user", default="admin",
                            help="Username to seed under (default: admin).")
        parser.add_argument("--count", type=int, default=12,
                            help="Orders per category (default: 12).")
        parser.add_argument("--seed", type=int, default=None,
                            help="RNG seed for deterministic output.")

    @transaction.atomic
    def handle(self, *args, **opts):
        rng = random.Random(opts["seed"])
        User = get_user_model()
        try:
            user = User.objects.get(username=opts["user"])
        except User.DoesNotExist:
            self.stderr.write(f"User '{opts['user']}' not found.")
            return

        # Ensure the user has at least one Store of each platform so we can
        # showcase both Shopify and WooCommerce orders.
        stores = []
        for platform, name, url in [
            ("shopify",     "Drop Sigma Test Shopify",   "https://dropsigma-demo.myshopify.com"),
            ("woocommerce", "Drop Sigma Test WooCommerce", "https://demo.dropsigma.com"),
        ]:
            store, created = Store.objects.get_or_create(
                user=user, platform=platform,
                defaults={"name": name, "store_url": url, "is_active": True},
            )
            stores.append(store)
            if created:
                self.stdout.write(f"  · Created store: {name}")

        now = timezone.now()
        per_cat = opts["count"]
        total_created = 0

        for cat in CATEGORIES:
            for i in range(per_cat):
                # Skip if this exact (user, category, slot) was seeded before.
                marker = f"seed_sourcing_orders:{cat}:{i}"
                if Order.objects.filter(
                    store__user=user,
                    raw_data__seed_marker=marker,
                ).exists():
                    continue

                store = rng.choice(stores)
                customer = rng.choice(CUSTOMERS)

                # 30% of orders are multi-line — exercises the "+N more items" pill
                num_items = rng.choices([1, 2, 3, 4], weights=[70, 18, 8, 4])[0]
                line_items = []
                total_price = Decimal("0.00")
                for _ in range(num_items):
                    template = rng.choice(PRODUCT_TEMPLATES)
                    qty = rng.choices([1, 2, 3], weights=[80, 15, 5])[0]
                    item = _build_line_item(rng, template, qty)
                    line_items.append(item)
                    total_price += Decimal(item["total"])

                # The merchant's customer-facing total (what the END customer
                # paid). Drop Sigma's sourcing quote is cheaper.
                merchant_total = total_price
                sourcing_total = (merchant_total * Decimal("0.70")).quantize(Decimal("0.01"))
                # Realistic split: product is ~75-85% of the quote, shipping
                # the remainder. Only populated once we've moved past
                # Pending Source (the team has actually quoted it).
                if cat != "pending_source":
                    shipping_ratio = Decimal(str(round(rng.uniform(0.15, 0.28), 2)))
                    sourcing_shipping = (sourcing_total * shipping_ratio).quantize(Decimal("0.01"))
                    sourcing_product = (sourcing_total - sourcing_shipping).quantize(Decimal("0.01"))
                else:
                    sourcing_product = None
                    sourcing_shipping = None
                    # Pending Source has no quote yet — wipe the placeholder
                    # total so the UI shows "—" instead of leaking a guess.
                    sourcing_total = None

                raw = _build_raw_data(rng, store.platform, customer, line_items, float(merchant_total))
                raw["seed_marker"] = marker

                first_line = line_items[0]
                external_id = f"{1000 + total_created + i + rng.randint(0, 99)}"
                created_at = now - timedelta(days=rng.randint(0, 60),
                                             hours=rng.randint(0, 23))
                # Per-category state machinery.
                kwargs = dict(
                    store=store,
                    external_order_id=f"DS-DEMO-{external_id}",
                    customer_name=customer[0],
                    customer_email=customer[1],
                    customer_phone=customer[2],
                    city=customer[4],
                    country=customer[7],
                    total_price=merchant_total,
                    currency="USD",
                    product_id=str(first_line["id"]),
                    product_name=first_line["product_name"],
                    payment_status="paid",
                    fulfillment_status="unfulfilled",
                    raw_data=raw,
                    sourcing_total_usd=sourcing_total,
                    sourcing_product_usd=sourcing_product,
                    sourcing_shipping_usd=sourcing_shipping,
                    sourcing_quoted_at=(created_at + timedelta(hours=rng.randint(1, 6))
                                        if cat != "pending_source" else None),
                    sourcing_lead_days=rng.randint(5, 14) if cat != "pending_source" else None,
                    sourcing_status=cat,
                )
                if cat in ("processing", "shipping", "delivered"):
                    kwargs["sourcing_paid_at"] = created_at + timedelta(hours=rng.randint(6, 24))
                if cat in ("shipping", "delivered"):
                    carrier, url_tpl = rng.choice(TRACKING_CARRIERS)
                    tnum = "".join(rng.choices("0123456789", k=12))
                    kwargs["tracking_company"] = carrier
                    kwargs["tracking_number"] = tnum
                    kwargs["tracking_url"] = url_tpl.format(tnum)
                    kwargs["fulfillment_status"] = "fulfilled" if cat == "delivered" else "partial"
                    kwargs["sourcing_shipped_at"] = created_at + timedelta(days=rng.randint(2, 6))
                if cat == "delivered":
                    delivered = created_at + timedelta(days=rng.randint(7, 14))
                    kwargs["delivered_at"] = delivered
                    kwargs["sourcing_delivered_at"] = delivered
                if cat == "cancel":
                    kwargs["sourcing_cancelled_at"] = created_at + timedelta(hours=rng.randint(2, 24))
                    kwargs["payment_status"] = "voided"

                o = Order.objects.create(**kwargs)
                # Re-stamp created_at so the seed spans the last 2 months
                # instead of all having today's timestamp.
                Order.objects.filter(pk=o.pk).update(created_at=created_at)
                total_created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Created {total_created} sample orders for '{user.username}' "
            f"({per_cat}/category, {len(CATEGORIES)} categories)."
        ))

        # Quick summary for verification.
        from collections import Counter
        breakdown = Counter(Order.objects.filter(store__user=user)
                            .values_list("sourcing_status", flat=True))
        self.stdout.write("\nFinal breakdown:")
        for cat in CATEGORIES:
            self.stdout.write(f"  {cat:<18} {breakdown.get(cat, 0)}")
        self.stdout.write(f"  {'TOTAL':<18} {sum(breakdown.values())}")

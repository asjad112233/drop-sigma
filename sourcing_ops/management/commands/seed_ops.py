"""Seed the Drop Sigma Operations Portal with starter data.

Creates roles, a sample ops team, a curated supplier network, sample
SupplierProducts for auto-detect, and grants ops access to the `admin`
user so the portal is immediately testable.

Usage:
    ./venv/bin/python manage.py seed_ops
    ./venv/bin/python manage.py seed_ops --reset   # wipe and re-seed
"""
import random
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from sourcing_ops.models import (
    Supplier, SupplierContact, SupplierProduct,
    OpsRole, OpsTeamMember, OpsAssignment, OrderOpsState, OpsActivity,
)
from sourcing_ops.permissions import OPS_GROUP_NAME, ensure_ops_group


ROLES = [
    ("Sourcing Specialist",  "sourcing-specialist", "Finds suppliers + quotes new orders", "#6366f1", 10),
    ("Procurement Lead",     "procurement-lead",    "Sends POs, tracks production",        "#a855f7", 20),
    ("QC Inspector",         "qc-inspector",        "Pre-shipment quality inspection",     "#10b981", 30),
    ("Shipping Coordinator", "shipping-coordinator","Carrier booking + tracking updates",  "#f59e0b", 40),
    ("Account Manager",      "account-manager",     "Tenant communications + escalations", "#ec4899", 50),
    ("Operations Manager",   "operations-manager",  "Team lead · sees everything",         "#d4af37", 100),
]

TEAM = [
    ("ops_amir",     "Amir Hussain",     "sourcing-specialist", "👨🏽", "active",  False),
    ("ops_jia",      "Jia Wen Li",       "sourcing-specialist", "👩🏻", "active",  False),
    ("ops_kareem",   "Kareem Al-Sayed",  "procurement-lead",    "👨🏽", "active",  False),
    ("ops_priya",    "Priya Iyer",       "qc-inspector",        "👩🏽", "active",  False),
    ("ops_ahmed",    "Ahmed Reza",       "shipping-coordinator","👨🏽", "active",  False),
    ("ops_sara",     "Sara Khan",        "account-manager",     "👩🏽", "active",  False),
    ("ops_omar",     "Omar Farooq",      "operations-manager",  "👨🏽", "active",  True),
]

SUPPLIERS = [
    {
        "name": "Shenzhen Velocity Co.", "tier": "preferred",
        "country": "China", "city": "Shenzhen", "address": "Building 4, Bao'an District",
        "contact_name": "Lin Wei", "contact_role": "Sales Director",
        "email": "lin.wei@sz-velocity.com", "phone": "+86-755-2811-9202",
        "wechat": "linwei_velocity", "whatsapp": "+86-138-2811-9202",
        "languages": ["English", "Mandarin", "Cantonese"],
        "default_lead_days": 5, "moq": 200, "payment_terms": "30_70",
        "payment_methods": ["T/T", "PayPal", "Alipay"],
        "specialties": ["Electronics", "Accessories", "Wearables"],
        "rating": Decimal("4.9"), "on_time_pct": 97,
        "cover_emoji": "⚡", "cover_gradient_from": "#6366f1", "cover_gradient_to": "#a855f7",
        "alibaba_url": "https://alibaba.com/sz-velocity",
        "products": [
            {"sku": "ELC-HUB-820", "name": "USB-C 7-in-1 Multi Hub", "variant_summary": "Space Gray / Silver", "cost_usd": "8.40", "ship_usd": "1.20", "lead_days": 5, "moq": 100},
            {"sku": "AUD-BUD-499", "name": "Wireless Bluetooth Earbuds Pro", "variant_summary": "3 colors · ANC", "cost_usd": "14.20", "ship_usd": "1.85", "lead_days": 6, "moq": 200},
            {"sku": "ELC-STD-708", "name": "Adjustable Phone Stand", "variant_summary": "3 colors · alum", "cost_usd": "3.80", "ship_usd": "0.95", "lead_days": 4, "moq": 100},
        ],
    },
    {
        "name": "Guangzhou Thread House", "tier": "preferred",
        "country": "China", "city": "Guangzhou", "address": "Block 12, Baiyun Apparel Hub",
        "contact_name": "Mei Zhang", "contact_role": "Account Manager",
        "email": "mei@gz-thread.com", "phone": "+86-20-3839-4127", "wechat": "mei_thread",
        "languages": ["English", "Mandarin"],
        "default_lead_days": 8, "moq": 150, "payment_terms": "30_70",
        "payment_methods": ["T/T", "PayPal"],
        "specialties": ["Apparel", "Footwear", "Accessories"],
        "rating": Decimal("4.8"), "on_time_pct": 95,
        "cover_emoji": "👕", "cover_gradient_from": "#ec4899", "cover_gradient_to": "#f59e0b",
        "products": [
            {"sku": "TSH-CRW-519", "name": "Cotton Crew T-Shirt", "variant_summary": "Size S-XL · 4 colors", "cost_usd": "6.50", "ship_usd": "1.10", "lead_days": 8, "moq": 100},
            {"sku": "BAG-TOT-573", "name": "Minimalist Canvas Tote Bag", "variant_summary": "Natural / Black / Olive", "cost_usd": "4.85", "ship_usd": "1.35", "lead_days": 7, "moq": 150},
            {"sku": "BAG-TOT-867", "name": "Minimalist Canvas Tote Bag", "variant_summary": "Natural / 14oz", "cost_usd": "5.20", "ship_usd": "1.45", "lead_days": 7, "moq": 100},
        ],
    },
    {
        "name": "Yiwu Bazaar Direct", "tier": "preferred",
        "country": "China", "city": "Yiwu", "address": "Futian Market District 5",
        "contact_name": "Chen Hao", "contact_role": "Owner",
        "email": "chen@yiwu-bazaar.cn", "phone": "+86-579-8551-2024", "wechat": "chen_yiwu",
        "languages": ["English", "Mandarin", "Spanish"],
        "default_lead_days": 10, "moq": 100, "payment_terms": "50_50",
        "payment_methods": ["T/T", "Alipay"],
        "specialties": ["Home Goods", "Toys", "Kitchen", "Stationery"],
        "rating": Decimal("4.7"), "on_time_pct": 92,
        "cover_emoji": "🛍️", "cover_gradient_from": "#10b981", "cover_gradient_to": "#06b6d4",
        "products": [
            {"sku": "HOM-CFE-710", "name": "Ceramic Pour-Over Coffee Set", "variant_summary": "1/2/4-cup · 3 colors", "cost_usd": "9.80", "ship_usd": "2.40", "lead_days": 10, "moq": 50},
            {"sku": "HOM-CFE-346", "name": "Ceramic Pour-Over Coffee Set", "variant_summary": "4-cup / Matte White", "cost_usd": "11.20", "ship_usd": "2.85", "lead_days": 10, "moq": 50},
            {"sku": "HOM-CAN-401", "name": "Organic Soy Candle Travel Tin", "variant_summary": "Scents · 4oz/8oz", "cost_usd": "2.95", "ship_usd": "1.15", "lead_days": 9, "moq": 200},
            {"sku": "HOM-PLW-129", "name": "Linen Throw Pillow Cover", "variant_summary": "Sizes / Colors", "cost_usd": "4.20", "ship_usd": "1.05", "lead_days": 8, "moq": 100},
        ],
    },
    {
        "name": "Ningbo Beauty Supply", "tier": "preferred",
        "country": "China", "city": "Ningbo", "address": "Hi-Tech Park, Building 7",
        "contact_name": "Sun Li", "contact_role": "Sales Manager",
        "email": "sun.li@nb-beauty.com", "phone": "+86-574-8721-3640", "wechat": "sun_beauty",
        "languages": ["English", "Mandarin", "Korean"],
        "default_lead_days": 14, "moq": 300, "payment_terms": "30_70",
        "payment_methods": ["T/T", "PayPal"],
        "specialties": ["Cosmetics", "Skincare", "Personal Care"],
        "rating": Decimal("4.6"), "on_time_pct": 96,
        "cover_emoji": "💄", "cover_gradient_from": "#f43f5e", "cover_gradient_to": "#f97316",
        "products": [
            {"sku": "BEA-KYL-995", "name": "Kayali Vanilla 28 EDP (50ml)", "variant_summary": "50ml", "cost_usd": "18.50", "ship_usd": "3.20", "lead_days": 14, "moq": 100},
            {"sku": "BEA-KYL-507", "name": "Kayali Vanilla 28 EDP (100ml)", "variant_summary": "100ml", "cost_usd": "29.00", "ship_usd": "4.50", "lead_days": 14, "moq": 100},
            {"sku": "BEA-SKN-220", "name": "Hydrating Skincare Set 3-Pack", "variant_summary": "Dry/Oily/Combo/Sensitive", "cost_usd": "22.40", "ship_usd": "3.80", "lead_days": 12, "moq": 50},
        ],
    },
    {
        "name": "Dongguan Injection Works", "tier": "standard",
        "country": "China", "city": "Dongguan",
        "contact_name": "Wong Tao", "email": "tao@dg-injection.com", "phone": "+86-769-2287-1140",
        "wechat": "wong_dg", "languages": ["English", "Mandarin"],
        "default_lead_days": 21, "moq": 500, "payment_terms": "30_70",
        "specialties": ["Plastics", "Custom Manufacturing", "Tooling"],
        "rating": Decimal("4.5"), "on_time_pct": 94,
        "cover_emoji": "⚙️", "cover_gradient_from": "#0ea5e9", "cover_gradient_to": "#6366f1",
        "products": [],
    },
    {
        "name": "Xiamen Home & Decor", "tier": "standard",
        "country": "China", "city": "Xiamen",
        "contact_name": "Liu Mei", "email": "mei.liu@xm-home.com", "phone": "+86-592-2208-7733",
        "wechat": "mei_xm", "languages": ["English", "Mandarin"],
        "default_lead_days": 9, "moq": 200, "payment_terms": "30_70",
        "specialties": ["Home Decor", "Garden", "Lighting"],
        "rating": Decimal("4.6"), "on_time_pct": 93,
        "cover_emoji": "🏠", "cover_gradient_from": "#a855f7", "cover_gradient_to": "#ec4899",
        "products": [
            {"sku": "FIT-YGA-216", "name": "Premium Yoga Mat", "variant_summary": "Thickness 6-10mm · 4 colors", "cost_usd": "7.40", "ship_usd": "2.10", "lead_days": 9, "moq": 100},
            {"sku": "FIT-YGA-597", "name": "Premium Yoga Mat", "variant_summary": "8mm / Teal", "cost_usd": "7.40", "ship_usd": "2.10", "lead_days": 9, "moq": 100},
        ],
    },
    {
        "name": "Shanghai Techlogix", "tier": "preferred",
        "country": "China", "city": "Shanghai",
        "contact_name": "Wang Hui", "email": "hui.wang@sh-techlogix.com", "phone": "+86-21-6332-4400",
        "wechat": "wang_sh", "languages": ["English", "Mandarin", "Japanese"],
        "default_lead_days": 6, "moq": 400, "payment_terms": "30_70",
        "specialties": ["Premium Electronics", "Bonded Warehouse"],
        "rating": Decimal("4.7"), "on_time_pct": 96,
        "cover_emoji": "🚀", "cover_gradient_from": "#1e40af", "cover_gradient_to": "#7c3aed",
        "products": [],
    },
    {
        "name": "Istanbul Style Co.", "tier": "standard",
        "country": "Turkey", "city": "Istanbul",
        "contact_name": "Emre Yılmaz", "email": "emre@istanbul-style.tr",
        "phone": "+90-212-555-0188", "whatsapp": "+90-532-555-0188",
        "languages": ["English", "Turkish", "German"],
        "default_lead_days": 12, "moq": 250, "payment_terms": "50_50",
        "payment_methods": ["T/T", "PayPal"],
        "specialties": ["Apparel", "Denim", "EU Market"],
        "rating": Decimal("4.7"), "on_time_pct": 95,
        "cover_emoji": "🇹🇷", "cover_gradient_from": "#dc2626", "cover_gradient_to": "#f59e0b",
        "products": [],
    },
]


class Command(BaseCommand):
    help = "Seed roles, team, and supplier network for the Operations Portal."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true",
                            help="Wipe seeded data (roles, team, suppliers) and re-seed.")

    @transaction.atomic
    def handle(self, *args, **opts):
        User = get_user_model()

        if opts["reset"]:
            self.stdout.write("  · resetting seeded ops data")
            Supplier.objects.all().delete()
            OpsTeamMember.objects.all().delete()
            OpsRole.objects.all().delete()

        # ── Roles ────────────────────────────────────────────────────
        roles_map = {}
        for name, slug, desc, color, sort in ROLES:
            r, _ = OpsRole.objects.update_or_create(
                slug=slug,
                defaults=dict(name=name, description=desc, color=color, sort_order=sort),
            )
            roles_map[slug] = r
        self.stdout.write(f"  · {len(roles_map)} roles in place")

        # ── Ops group + admin ────────────────────────────────────────
        ops_group = ensure_ops_group()
        admin = User.objects.filter(username="admin").first()
        if admin:
            admin.groups.add(ops_group)
            # Make admin an Operations Manager so they see everything
            OpsTeamMember.objects.update_or_create(
                user=admin,
                defaults=dict(
                    display_name=admin.get_full_name() or "Admin (Ops Lead)",
                    role=roles_map.get("operations-manager"),
                    title="Operations Lead",
                    avatar_emoji="Σ",
                    avatar_color="#d4af37",
                    status="active",
                    is_manager=True,
                    can_assign=True,
                    can_quote=True,
                ),
            )
            self.stdout.write(f"  · admin user granted Ops Manager access")

        # ── Team members ─────────────────────────────────────────────
        team_created = 0
        for username, display_name, role_slug, emoji, status, is_manager in TEAM:
            user, _ = User.objects.get_or_create(
                username=username,
                defaults=dict(email=f"{username}@dropsigma.com", first_name=display_name.split(" ")[0]),
            )
            if not user.has_usable_password():
                user.set_password("dropsigma123")
                user.save()
            user.groups.add(ops_group)
            OpsTeamMember.objects.update_or_create(
                user=user,
                defaults=dict(
                    display_name=display_name,
                    role=roles_map.get(role_slug),
                    title=roles_map[role_slug].name if role_slug in roles_map else "",
                    avatar_emoji=emoji,
                    avatar_color=roles_map[role_slug].color if role_slug in roles_map else "#6366f1",
                    status=status,
                    is_manager=is_manager,
                    can_assign=True,
                    can_quote=role_slug in ("sourcing-specialist", "operations-manager", "account-manager"),
                    on_time_pct=random.randint(92, 99),
                ),
            )
            team_created += 1
        self.stdout.write(f"  · {team_created} ops team members ready (password: dropsigma123)")

        # ── Suppliers + products ─────────────────────────────────────
        sup_created = 0
        product_created = 0
        for s in SUPPLIERS:
            slug = slugify(s["name"])
            sup, _ = Supplier.objects.update_or_create(
                slug=slug,
                defaults={k: v for k, v in s.items() if k not in ("products",)},
            )
            sup_created += 1
            # Primary contact
            if sup.contact_name:
                SupplierContact.objects.update_or_create(
                    supplier=sup, name=sup.contact_name,
                    defaults=dict(
                        role=sup.contact_role,
                        email=sup.email, phone=sup.phone, whatsapp=sup.whatsapp,
                        is_primary=True,
                    ),
                )
            # Products
            for p in s.get("products", []):
                cost = Decimal(p["cost_usd"])
                ship = Decimal(p["ship_usd"])
                # Drop Sigma marks up ~40% on cost for the tenant suggestion
                suggested = (cost * Decimal("1.40")).quantize(Decimal("0.01"))
                SupplierProduct.objects.update_or_create(
                    supplier=sup, sku=p["sku"],
                    defaults=dict(
                        product_name=p["name"],
                        variant_summary=p.get("variant_summary", ""),
                        unit_cost_usd=cost,
                        shipping_unit_usd=ship,
                        moq=p["moq"],
                        lead_days=p["lead_days"],
                        suggested_tenant_price_usd=suggested,
                        is_preferred=True,
                        is_in_stock=True,
                    ),
                )
                product_created += 1

        self.stdout.write(f"  · {sup_created} suppliers · {product_created} catalog products linked")

        # ── Initial activity ─────────────────────────────────────────
        from orders.models import Order
        sample_orders = Order.objects.all()[:3]
        for o in sample_orders:
            OpsActivity.objects.get_or_create(
                order=o,
                kind="system",
                title="Order arrived from tenant store",
                defaults=dict(
                    detail=f"Synced from {getattr(o.store,'platform','')} — awaiting ops triage.",
                    icon="📥",
                    actor_label="System",
                ),
            )

        self.stdout.write(self.style.SUCCESS("\n✓ Operations Portal seeded.\n"))
        self.stdout.write("Login as admin / admin1234 → visit /ops/")

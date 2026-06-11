"""Seed 8 realistic DropSigma Sourcing Partners. Idempotent."""
from decimal import Decimal
from django.core.management.base import BaseCommand
from sourcing_partners.models import SourcingPartner


PARTNERS = [
    {
        "slug": "shenzhen-velocity",
        "name": "Shenzhen Velocity Co.",
        "cover_emoji": "⚡",
        "cover_gradient_from": "#6366f1",
        "cover_gradient_to":   "#a855f7",
        "tagline": "Fast electronics sourcing · 48h quote SLA",
        "description": (
            "Shenzhen Velocity is a DropSigma top-tier partner specialising in "
            "consumer electronics, accessories, and small gadgets. Direct lines "
            "into 200+ verified factories in Huaqiangbei. Best fit for tech "
            "merchants needing fast quotes and reliable QC."
        ),
        "country": "China",
        "headquarters": "Shenzhen, Guangdong",
        "founded_year": 2016,
        "specialties": ["Electronics", "Accessories", "Wearables", "Lighting"],
        "languages": ["English", "Mandarin", "Cantonese"],
        "payment_methods": ["Wise", "Alipay", "Stripe", "Bank Transfer"],
        "min_order_value_usd": 200,
        "typical_lead_days": 5,
        "team_size": "80–120",
        "monthly_volume": "3,500+ orders",
        "rating": Decimal("4.92"),
        "total_reviews": 287,
        "total_orders": 18420,
        "on_time_rate_pct": 97,
        "avg_response_hours": 2,
        "is_verified": True, "is_featured": True, "sort_order": 1,
    },
    {
        "slug": "guangzhou-thread-house",
        "name": "Guangzhou Thread House",
        "cover_emoji": "👕",
        "cover_gradient_from": "#ec4899",
        "cover_gradient_to":   "#f59e0b",
        "tagline": "Apparel & accessories · custom labels & tags",
        "description": (
            "Premium garment sourcing from Guangzhou's textile hub. Specialists "
            "in streetwear, athleisure, and accessories. Full white-label "
            "service with custom hangtags, woven labels, and dust bags."
        ),
        "country": "China",
        "headquarters": "Guangzhou, Guangdong",
        "founded_year": 2014,
        "specialties": ["Apparel", "Footwear", "Bags", "Accessories"],
        "languages": ["English", "Mandarin"],
        "payment_methods": ["Wise", "Alipay", "Bank Transfer"],
        "min_order_value_usd": 150,
        "typical_lead_days": 8,
        "team_size": "120–180",
        "monthly_volume": "5,000+ orders",
        "rating": Decimal("4.88"),
        "total_reviews": 412,
        "total_orders": 26100,
        "on_time_rate_pct": 95,
        "avg_response_hours": 3,
        "is_verified": True, "is_featured": True, "sort_order": 2,
    },
    {
        "slug": "yiwu-bazaar-direct",
        "name": "Yiwu Bazaar Direct",
        "cover_emoji": "🛍️",
        "cover_gradient_from": "#10b981",
        "cover_gradient_to":   "#06b6d4",
        "tagline": "Yiwu market consolidation · 500K+ SKUs catalogued",
        "description": (
            "Direct access to Yiwu — the world's largest small-commodities "
            "market. We consolidate orders across 200+ vendors into a single "
            "shipment, slashing freight cost up to 60%."
        ),
        "country": "China",
        "headquarters": "Yiwu, Zhejiang",
        "founded_year": 2012,
        "specialties": ["Home Goods", "Toys", "Kitchen", "Stationery", "Gifts"],
        "languages": ["English", "Mandarin", "Spanish"],
        "payment_methods": ["Wise", "Alipay", "PayPal"],
        "min_order_value_usd": 100,
        "typical_lead_days": 10,
        "team_size": "200+",
        "monthly_volume": "8,000+ orders",
        "rating": Decimal("4.75"),
        "total_reviews": 519,
        "total_orders": 41200,
        "on_time_rate_pct": 92,
        "avg_response_hours": 4,
        "is_verified": True, "is_featured": True, "sort_order": 3,
    },
    {
        "slug": "ningbo-beauty-supply",
        "name": "Ningbo Beauty Supply",
        "cover_emoji": "💄",
        "cover_gradient_from": "#f43f5e",
        "cover_gradient_to":   "#f97316",
        "tagline": "Skincare, cosmetics, personal care · GMP-certified",
        "description": (
            "GMP-certified cosmetic and skincare sourcing. Direct partnerships "
            "with FDA-compliant factories in Ningbo and Shanghai. Full private-"
            "label and OEM capabilities for emerging beauty brands."
        ),
        "country": "China",
        "headquarters": "Ningbo, Zhejiang",
        "founded_year": 2018,
        "specialties": ["Cosmetics", "Skincare", "Personal Care", "Haircare"],
        "languages": ["English", "Mandarin", "Korean"],
        "payment_methods": ["Wise", "Stripe", "Bank Transfer"],
        "min_order_value_usd": 300,
        "typical_lead_days": 14,
        "team_size": "40–60",
        "monthly_volume": "1,200+ orders",
        "rating": Decimal("4.85"),
        "total_reviews": 167,
        "total_orders": 7820,
        "on_time_rate_pct": 96,
        "avg_response_hours": 3,
        "is_verified": True, "sort_order": 4,
    },
    {
        "slug": "dongguan-injection-works",
        "name": "Dongguan Injection Works",
        "cover_emoji": "⚙️",
        "cover_gradient_from": "#0ea5e9",
        "cover_gradient_to":   "#6366f1",
        "tagline": "Custom plastic & metal manufacturing · in-house tooling",
        "description": (
            "Vertically integrated factory with injection moulding, CNC, and "
            "in-house tooling. Best for custom products: phone cases, gadgets, "
            "household items. Tooling lead time 18–25 days."
        ),
        "country": "China",
        "headquarters": "Dongguan, Guangdong",
        "founded_year": 2010,
        "specialties": ["Plastics", "Custom Manufacturing", "Tooling"],
        "languages": ["English", "Mandarin"],
        "payment_methods": ["Wise", "Bank Transfer"],
        "min_order_value_usd": 500,
        "typical_lead_days": 21,
        "team_size": "300+",
        "monthly_volume": "600+ projects",
        "rating": Decimal("4.79"),
        "total_reviews": 94,
        "total_orders": 3540,
        "on_time_rate_pct": 94,
        "avg_response_hours": 6,
        "is_verified": True, "sort_order": 5,
    },
    {
        "slug": "xiamen-home-decor",
        "name": "Xiamen Home & Decor",
        "cover_emoji": "🏠",
        "cover_gradient_from": "#a855f7",
        "cover_gradient_to":   "#ec4899",
        "tagline": "Home, garden, lifestyle · curated trend catalogue",
        "description": (
            "Curated home and lifestyle products with a strong design sense. "
            "Our trend scouts surface viral products before they hit "
            "TikTok. Best fit for design-forward dropshippers."
        ),
        "country": "China",
        "headquarters": "Xiamen, Fujian",
        "founded_year": 2017,
        "specialties": ["Home Decor", "Garden", "Lighting", "Kitchen"],
        "languages": ["English", "Mandarin"],
        "payment_methods": ["Wise", "Alipay", "Bank Transfer"],
        "min_order_value_usd": 200,
        "typical_lead_days": 9,
        "team_size": "60–90",
        "monthly_volume": "2,200+ orders",
        "rating": Decimal("4.83"),
        "total_reviews": 198,
        "total_orders": 11650,
        "on_time_rate_pct": 93,
        "avg_response_hours": 4,
        "is_verified": True, "sort_order": 6,
    },
    {
        "slug": "shanghai-techlogix",
        "name": "Shanghai Techlogix",
        "cover_emoji": "🚀",
        "cover_gradient_from": "#1e40af",
        "cover_gradient_to":   "#7c3aed",
        "tagline": "Premium electronics + air freight optimisation",
        "description": (
            "Premium electronics sourcing with bonded warehouse access. "
            "Our logistics arm consolidates air freight from Shanghai to "
            "USA/EU/AU at jaw-dropping rates — pass the savings to your "
            "customers."
        ),
        "country": "China",
        "headquarters": "Shanghai",
        "founded_year": 2015,
        "specialties": ["Premium Electronics", "Logistics", "Bonded Warehouse"],
        "languages": ["English", "Mandarin", "Japanese"],
        "payment_methods": ["Wise", "Stripe", "Bank Transfer"],
        "min_order_value_usd": 400,
        "typical_lead_days": 6,
        "team_size": "100–150",
        "monthly_volume": "2,800+ orders",
        "rating": Decimal("4.81"),
        "total_reviews": 156,
        "total_orders": 9420,
        "on_time_rate_pct": 96,
        "avg_response_hours": 3,
        "is_verified": True, "sort_order": 7,
    },
    {
        "slug": "istanbul-style-co",
        "name": "Istanbul Style Co.",
        "cover_emoji": "🇹🇷",
        "cover_gradient_from": "#dc2626",
        "cover_gradient_to":   "#f59e0b",
        "tagline": "Turkey-based apparel · faster EU/UK delivery",
        "description": (
            "Our only non-China partner. Turkey-based apparel manufacturer "
            "with EU customs union benefits — 50% faster delivery to UK and "
            "Europe vs Asian sourcing. Premium denim, knits, and outerwear."
        ),
        "country": "Turkey",
        "headquarters": "Istanbul",
        "founded_year": 2008,
        "specialties": ["Apparel", "Denim", "Knitwear", "EU Market"],
        "languages": ["English", "Turkish", "German"],
        "payment_methods": ["Wise", "Stripe", "Bank Transfer", "Swift"],
        "min_order_value_usd": 250,
        "typical_lead_days": 12,
        "team_size": "150–200",
        "monthly_volume": "1,800+ orders",
        "rating": Decimal("4.87"),
        "total_reviews": 124,
        "total_orders": 6280,
        "on_time_rate_pct": 95,
        "avg_response_hours": 5,
        "is_verified": True, "sort_order": 8,
    },
]


class Command(BaseCommand):
    help = "Seed the 8 DropSigma Sourcing Partners. Idempotent."

    def handle(self, *args, **opts):
        created = 0
        updated = 0
        for data in PARTNERS:
            obj, was_created = SourcingPartner.objects.update_or_create(
                slug=data["slug"],
                defaults={k: v for k, v in data.items() if k != "slug"},
            )
            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"✓ Seeded sourcing partners — created {created}, updated {updated}, "
            f"total active = {SourcingPartner.objects.filter(is_active=True).count()}"
        ))

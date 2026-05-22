"""
AI Training Studio v2 — constants + score helpers.

Single source of truth for:
  * The 52-question Q&A questionnaire across 10 sections
  * The 10 advanced topics (each = a category bucket of snippets)
  * Confidence-score calculation per topic and overall

`KnowledgeSnippet.category` stores the topic key (e.g. "Disputes"). The
per-topic enable/disable toggle lives in `AiTrainingProfile.toggles`
under the key `topic_<lower_key>_enabled`.
"""

from .models import AiTrainingProfile, KnowledgeSnippet


# ─── 52 Q&A Questions across 10 sections ───────────────────────────────────

QA_SECTIONS = [
    {
        "id":   "business_basics",
        "name": "Business Basics",
        "icon": "fa-briefcase",
        "questions": [
            {"id": 53, "title": "What is your business name?",
             "help": "The exact brand name customers should see in every AI reply.",
             "placeholder": "e.g. Kayali Fragrances"},
            {"id": 1, "title": "What does your business sell?",
             "help": "Niche, product categories, sample SKUs.",
             "placeholder": "e.g. Premium fragrances and beauty essentials curated from international brands."},
            {"id": 2, "title": "Who is your typical customer?",
             "help": "Age, location, interests, language preference.",
             "placeholder": "e.g. Women 25–45 in North America and Europe, fragrance enthusiasts."},
            {"id": 3, "title": "Which countries do you ship to?",
             "help": "List primary markets.",
             "placeholder": "e.g. USA, Canada, UK, Germany, France, Australia."},
            {"id": 4, "title": "What languages do you support?",
             "help": "Languages your team can reply in.",
             "placeholder": "e.g. English (primary), French, Arabic."},
            {"id": 5, "title": "What are your support hours?",
             "help": "Working days and time zone.",
             "placeholder": "e.g. Mon–Fri, 9 AM to 6 PM EST."},
            {"id": 6, "title": "What's your average reply-time SLA?",
             "help": "How fast you aim to respond.",
             "placeholder": "e.g. Within 2 business hours, never more than 24h."},
        ],
    },
    {
        "id":   "brand_voice",
        "name": "Brand Voice",
        "icon": "fa-palette",
        "questions": [
            {"id": 7, "title": "Email sign-off style?",
             "help": "How you close emails.",
             "placeholder": "e.g. Cheers, Asjad — never \"Sincerely\" or \"Regards\"."},
            {"id": 8, "title": "Phrases you'd never say?",
             "help": "Corporate jargon, awkward formal lines to avoid.",
             "placeholder": "e.g. \"As per our policy\", \"Kindly note\", \"Hereby\"."},
            {"id": 9, "title": "Do you use emojis? In what situations?",
             "help": "Empty if never; else describe.",
             "placeholder": "e.g. Yes, sparingly: ✨ when introducing a launch, 🙏 when apologizing."},
            {"id": 10, "title": "Share a sample reply that perfectly represents your brand voice.",
             "help": "Paste a recent email reply you wrote that you're proud of.",
             "placeholder": "Hi Sarah, thanks so much for reaching out…"},
            {"id": 11, "title": "Overall tone — friendly / formal / luxury / playful?",
             "help": "A few words describing the vibe.",
             "placeholder": "e.g. Warm but professional, like a knowledgeable friend."},
        ],
    },
    {
        "id":   "shipping",
        "name": "Shipping",
        "icon": "fa-truck-fast",
        "questions": [
            {"id": 12, "title": "What origin label to show customers?",
             "help": "Generic public-facing label (we hide actual origin city).",
             "placeholder": "e.g. International Warehouse, Global Fulfillment Network."},
            {"id": 13, "title": "Average delivery time by region?",
             "help": "Rough timelines per destination.",
             "placeholder": "e.g. US/CA: 8–12 days; EU: 10–14 days; AU: 12–16 days."},
            {"id": 14, "title": "Which couriers do you use?",
             "help": "Carrier list.",
             "placeholder": "e.g. DHL Express for premium, 4PX for standard, USPS final-mile."},
            {"id": 15, "title": "Expedited shipping available? Cost?",
             "help": "Faster option + extra charge.",
             "placeholder": "e.g. Express shipping +$15, arrives in 3–5 days."},
            {"id": 16, "title": "Your custom tracking page URL?",
             "help": "Branded tracking page link (e.g. dropsigma.com/track).",
             "placeholder": "e.g. https://dropsigma.com/track/{tracking_id}"},
            {"id": 17, "title": "Free shipping threshold?",
             "help": "Minimum order for free shipping.",
             "placeholder": "e.g. Free shipping on orders above $75."},
        ],
    },
    {
        "id":   "returns_refunds",
        "name": "Returns & Refunds",
        "icon": "fa-rotate-left",
        "questions": [
            {"id": 18, "title": "Return window (days)?",
             "help": "How many days after delivery customer can return.",
             "placeholder": "e.g. 30 days from delivery."},
            {"id": 19, "title": "Who pays for return shipping?",
             "help": "Customer or you.",
             "placeholder": "e.g. EU customers free; rest of world customer pays."},
            {"id": 20, "title": "Refund processing time?",
             "help": "How long after we receive the return.",
             "placeholder": "e.g. 5 business days back to original payment method."},
            {"id": 21, "title": "Items that are NOT returnable?",
             "help": "Final-sale, hygiene products, customized items.",
             "placeholder": "e.g. Opened perfumes, custom engravings, clearance items."},
            {"id": 22, "title": "Do you offer exchanges? How does it work?",
             "help": "Yes/no and process.",
             "placeholder": "e.g. Yes — customer returns original, we ship the exchange after item is received."},
            {"id": 23, "title": "What if the item arrives damaged?",
             "help": "Photo proof required, immediate replacement vs refund.",
             "placeholder": "e.g. Photo proof within 48h, full replacement free of charge."},
            {"id": 24, "title": "Restocking fee — any?",
             "help": "Yes/no + amount.",
             "placeholder": "e.g. None for unopened items; 15% for opened beauty products."},
        ],
    },
    {
        "id":   "cancellations",
        "name": "Cancellations",
        "icon": "fa-ban",
        "questions": [
            {"id": 25, "title": "Cancellation cutoff?",
             "help": "Window during which customer can cancel.",
             "placeholder": "e.g. Within 24 hours of placing the order, before warehouse processing."},
            {"id": 26, "title": "Cancellation fee — any?",
             "help": "Free or charged.",
             "placeholder": "e.g. Free if within cutoff window."},
            {"id": 27, "title": "What if customer cancels AFTER the order ships?",
             "help": "Refuse delivery, return process, restocking.",
             "placeholder": "e.g. Refuse delivery and we refund minus return shipping."},
            {"id": 28, "title": "Partial cancellation allowed?",
             "help": "Removing one item from multi-item order.",
             "placeholder": "e.g. Yes if not yet picked; we refund the removed item."},
        ],
    },
    {
        "id":   "address_changes",
        "name": "Address Changes",
        "icon": "fa-location-dot",
        "questions": [
            {"id": 29, "title": "Can customers change shipping address?",
             "help": "Window when address can be edited.",
             "placeholder": "e.g. Yes, only before order is marked Fulfilled."},
            {"id": 30, "title": "Can they change items / sizes / colors?",
             "help": "Item swap policy before shipping.",
             "placeholder": "e.g. Yes within 24h of order, if still in stock."},
            {"id": 31, "title": "What if customer entered wrong address?",
             "help": "Update if possible, redirect cost if shipped.",
             "placeholder": "e.g. Free update before fulfillment; $12 redirect fee if already shipped."},
        ],
    },
    {
        "id":   "disputes_complaints",
        "name": "Disputes & Complaints",
        "icon": "fa-gavel",
        "questions": [
            {"id": 32, "title": "How do you handle PayPal / Stripe disputes?",
             "help": "Tone, evidence flow.",
             "placeholder": "e.g. Acknowledge empathically, ask for what we can fix, offer refund or replacement before they escalate."},
            {"id": 33, "title": "Policy on negative public reviews?",
             "help": "How AI should respond when customer threatens a public review.",
             "placeholder": "e.g. Address the issue privately first; never threaten or pressure."},
            {"id": 34, "title": "When to offer goodwill discounts?",
             "help": "Triggers for offering a discount code.",
             "placeholder": "e.g. Delivery delays > 7 days, second-time customers with issues, packaging defects."},
            {"id": 35, "title": "Max compensation authorized per issue?",
             "help": "Hard cap the AI should never exceed.",
             "placeholder": "e.g. Up to 20% discount or $50 store credit per case; refunds always allowed."},
        ],
    },
    {
        "id":   "products_inventory",
        "name": "Products & Inventory",
        "icon": "fa-boxes-stacked",
        "questions": [
            {"id": 36, "title": "What if a product goes out of stock after order?",
             "help": "Replacement, refund, or wait.",
             "placeholder": "e.g. Notify within 24h, offer alternative or full refund."},
            {"id": 37, "title": "Pre-orders allowed? With what timeline?",
             "help": "Yes/no and lead time.",
             "placeholder": "e.g. Yes, ships within 3 weeks of order date."},
            {"id": 38, "title": "Sizing / fit guidance?",
             "help": "How to direct customers to sizing info.",
             "placeholder": "e.g. Direct to /sizing-guide; runs true to size; women size down 1 for slim fit."},
            {"id": 39, "title": "Care instructions for products?",
             "help": "Cleaning, storage, shelf life.",
             "placeholder": "e.g. Store fragrances away from light/heat; perfumes last 3–5 years unopened."},
            {"id": 40, "title": "Bulk / wholesale order handling?",
             "help": "Threshold and process.",
             "placeholder": "e.g. 10+ units: email wholesale@dropsigma.com for trade pricing."},
        ],
    },
    {
        "id":   "payment_pricing",
        "name": "Payment & Pricing",
        "icon": "fa-credit-card",
        "questions": [
            {"id": 41, "title": "Which payment methods do you accept?",
             "help": "Cards, wallets, BNPL.",
             "placeholder": "e.g. Visa/Mastercard/Amex via Stripe, PayPal, Apple Pay, Klarna 4-pay."},
            {"id": 42, "title": "Failed payment troubleshooting steps?",
             "help": "Common reasons + recovery flow.",
             "placeholder": "e.g. Suggest contacting bank (international block), try another card, switch to PayPal."},
            {"id": 43, "title": "When to offer discount codes?",
             "help": "Abandoned cart, first order, complaints.",
             "placeholder": "e.g. First-order WELCOME10, abandoned cart COMEBACK15, complaints SORRY10."},
            {"id": 44, "title": "Currency support?",
             "help": "Multi-currency display or USD only.",
             "placeholder": "e.g. USD-priced but charged in customer's local currency via Stripe."},
            {"id": 45, "title": "Tax / VAT handling?",
             "help": "Included or added at checkout.",
             "placeholder": "e.g. EU customers see VAT included; rest of world: tax added at checkout where required."},
        ],
    },
    {
        "id":   "loyalty_after",
        "name": "Loyalty, VIP & After-Sales",
        "icon": "fa-medal",
        "questions": [
            {"id": 46, "title": "Loyalty program — points or tiers?",
             "help": "Describe any rewards.",
             "placeholder": "e.g. 1 point per $1; 100 points = $5 off; tiers: Bronze/Silver/Gold."},
            {"id": 47, "title": "Returning-customer perks?",
             "help": "Free shipping, early access, etc.",
             "placeholder": "e.g. Free shipping for 3+ orders, early access to launches."},
            {"id": 48, "title": "How do you handle VIP customers?",
             "help": "Personal outreach, priority handling.",
             "placeholder": "e.g. Personal email from owner, priority Playwright tracking, hand-picked thank-you gift."},
            {"id": 49, "title": "Follow-up emails after delivery?",
             "help": "Thank-you, review request.",
             "placeholder": "e.g. Day 2: thank-you. Day 7: ask for review with 10% next-order code."},
            {"id": 50, "title": "Warranty period?",
             "help": "How long products are guaranteed.",
             "placeholder": "e.g. 90 days against defects, no questions asked."},
            {"id": 51, "title": "Replacement parts / accessories available?",
             "help": "Caps, sprayers, atomizers etc.",
             "placeholder": "e.g. Travel atomizers free with orders above $200; replacement caps on request."},
            {"id": 52, "title": "How do you collect customer feedback?",
             "help": "Survey, email, in-app review.",
             "placeholder": "e.g. Post-purchase email survey + automated review request after delivery."},
        ],
    },
]


# ─── Smart preset options per question ──────────────────────────────────────
# Each entry: { "options": [...], "multi": bool, "free_text_only": bool }
# Tenant can click an option to auto-fill (single) or toggle on/off (multi),
# and always remains free to type a custom answer in the textarea.

OPTIONS_MAP = {
    # Business Basics
    53: {"free_text_only": True},  # Business name — unique per tenant, no chips
    1:  {"options": ["Beauty / Cosmetics", "Fragrances / Perfumes", "Fashion / Apparel",
                     "Electronics / Gadgets", "Home & Living", "Health / Wellness",
                     "Food & Beverage", "Sports / Outdoor", "Toys / Kids",
                     "Jewelry / Accessories", "Pet products", "Books / Media"],
         "multi": True},
    2:  {"options": ["Women 25–45 in North America", "Men 18–35 globally",
                     "Affluent buyers in EU & UK", "Gen Z worldwide",
                     "Niche enthusiasts (collectors, hobbyists)",
                     "Small business / B2B buyers"]},
    3:  {"options": ["USA", "Canada", "UK", "Germany", "France", "Italy", "Spain",
                     "Netherlands", "Australia", "New Zealand", "UAE", "Saudi Arabia",
                     "Pakistan", "India", "Singapore", "Worldwide"],
         "multi": True},
    4:  {"options": ["English", "French", "Spanish", "German", "Italian",
                     "Arabic", "Urdu", "Hindi", "Portuguese", "Dutch", "Japanese"],
         "multi": True},
    5:  {"options": ["24 / 7 (always on)", "Mon–Fri, 9 AM – 6 PM",
                     "Mon–Sat, 9 AM – 6 PM", "Mon–Fri, 10 AM – 4 PM",
                     "Weekdays only, customer's local time"]},
    6:  {"options": ["Within 1 hour during support hours",
                     "Within 2 business hours",
                     "Within 4 business hours",
                     "Within 24 hours (always)",
                     "Within 48 hours (max)"]},

    # Brand Voice
    7:  {"options": ['Cheers, [Name]', "Best, [Name]", "Thanks, [Name]",
                     "Sincerely, [Name]", "Warmly, [Name]", "Take care, [Name]",
                     "Talk soon, [Name]"]},
    8:  {"options": ['"As per our policy"', '"Kindly note"', '"Hereby"',
                     '"At your earliest convenience"', '"Please be advised"',
                     '"Regret to inform"', '"Pursuant to"', "Slang / abbreviations"],
         "multi": True},
    9:  {"options": ["Never use emojis", "Sparingly — apologies (🙏) and celebrations (✨)",
                     "Frequently — keep it casual",
                     "Brand-specific emoji set only"]},
    10: {"free_text_only": True},
    11: {"options": ["Warm but professional", "Friendly and casual",
                     "Formal and corporate", "Playful and quirky",
                     "Luxury and refined", "Minimal and direct",
                     "Empathetic and supportive"],
         "multi": True},

    # Shipping
    12: {"options": ["International Warehouse", "Global Fulfillment Network",
                     "Our Partner Distribution Center", "From our flagship atelier",
                     "Curated supplier network"]},
    13: {"options": ["US/CA: 8–12 days · EU: 10–14 · AU: 12–16",
                     "Worldwide 7–14 business days",
                     "Domestic 3–5 days · International 10–20 days",
                     "Express only — 3–7 days globally"]},
    14: {"options": ["DHL Express", "FedEx", "UPS", "USPS", "Royal Mail",
                     "4PX", "YunExpress", "China Post", "Aramex",
                     "TCS Pakistan", "Leopards Courier", "Local national post"],
         "multi": True},
    15: {"options": ["No — standard shipping only",
                     "Yes — +$15 for 3–5 day express",
                     "Yes — +$25 for 2-day express",
                     "Yes — free express on orders over $200"]},
    16: {"options": ["https://dropsigma.com/track/{tracking_id}",
                     "Use the courier's own tracking page",
                     "Custom branded tracking domain"]},
    17: {"options": ["Always free shipping", "Free over $50", "Free over $75",
                     "Free over $100", "Free over $150",
                     "No free shipping — flat $9.99"]},

    # Returns & Refunds
    18: {"options": ["14 days", "30 days", "45 days", "60 days", "90 days",
                     "No returns accepted"]},
    19: {"options": ["We pay (free returns worldwide)",
                     "Customer pays return shipping",
                     "EU customers free, rest pay",
                     "Customer pays unless item was wrong/damaged"]},
    20: {"options": ["Within 24 hours of receiving the return",
                     "Within 3 business days",
                     "Within 5 business days",
                     "Within 7–10 business days"]},
    21: {"options": ["Final sale items", "Opened beauty / perfumes",
                     "Hygiene products (underwear, swimwear)",
                     "Customized / engraved items", "Sale / clearance",
                     "Digital products", "Gift cards",
                     "Items missing original packaging"],
         "multi": True},
    22: {"options": ["Yes — direct exchange (free)",
                     "Yes — refund + place new order",
                     "Same-size/color exchanges only",
                     "No exchanges — refund only"]},
    23: {"options": ["Photo proof + full refund within 24h",
                     "Photo proof + free replacement",
                     "Customer returns first, then refund",
                     "Replacement after damage assessment"]},
    24: {"options": ["None — never charged",
                     "10% restocking fee",
                     "15% restocking fee",
                     "20% restocking fee",
                     "Only for opened beauty items"]},

    # Cancellations
    25: {"options": ["Within 1 hour of placing the order",
                     "Within 12 hours",
                     "Within 24 hours",
                     "Before warehouse processing (typically 24h)",
                     "Anytime before shipping"]},
    26: {"options": ["Free if within cutoff window",
                     "Flat $5 cancellation fee",
                     "10% of order value",
                     "No fee — fully refundable"]},
    27: {"options": ["Refuse delivery — full refund minus return shipping",
                     "Accept delivery, then initiate return",
                     "No refund after shipment",
                     "Customer keeps item, we refund 80%"]},
    28: {"options": ["Yes — if not yet picked from warehouse",
                     "Yes — anytime before shipping",
                     "No — only full-order cancellations",
                     "Yes but with $5 admin fee"]},

    # Address Changes
    29: {"options": ["Yes — only before order is fulfilled",
                     "Yes — within 24 hours of placing order",
                     "Yes — anytime before package leaves warehouse",
                     "No changes allowed after order placed"]},
    30: {"options": ["Yes — within 24h, if stock allows",
                     "Yes — anytime before shipping",
                     "Only size/color swap, no item change",
                     "No item changes after order placed"]},
    31: {"options": ["Free update if before fulfillment, redirect fee $12 if shipped",
                     "Free update anytime before package leaves",
                     "Customer responsibility — we cannot redirect",
                     "Reship to correct address at customer's cost"]},

    # Disputes & Complaints
    32: {"options": ["Acknowledge empathically, offer refund/replacement before they escalate",
                     "Gather evidence (tracking, photos) and fight the dispute",
                     "Refund immediately, then negotiate goodwill",
                     "Escalate to senior staff for any dispute"]},
    33: {"options": ["Respond publicly with empathy, then resolve privately",
                     "Reply privately first, ask to update review when resolved",
                     "Public response only, no private outreach",
                     "Do not engage with public reviews"]},
    34: {"options": ["Delivery delays > 7 days",
                     "Multiple issues with same order",
                     "Returning customer with a problem",
                     "Damaged item",
                     "Customer service mistakes",
                     "Holiday / seasonal goodwill"],
         "multi": True},
    35: {"options": ["Up to 10% discount or $25 credit",
                     "Up to 20% discount or $50 credit",
                     "Up to 30% discount or $100 credit",
                     "Full refund authorized; no further compensation cap",
                     "Refunds only — no extra compensation"]},

    # Products & Inventory
    36: {"options": ["Notify within 24h, offer alternative or full refund",
                     "Notify customer, hold order until restock",
                     "Auto-refund immediately, send apology email",
                     "Wait 7 days for restock, then refund if no resolution"]},
    37: {"options": ["Yes — ships within 2 weeks",
                     "Yes — ships within 3–4 weeks",
                     "Yes — custom timeline per product",
                     "No pre-orders accepted"]},
    38: {"free_text_only": True},
    39: {"free_text_only": True},
    40: {"options": ["Email wholesale@yourstore.com for 10+ unit pricing",
                     "Bulk pricing for 25+ units",
                     "B2B portal — separate account required",
                     "No bulk orders accepted"]},

    # Payment & Pricing
    41: {"options": ["Visa / Mastercard", "American Express", "PayPal",
                     "Apple Pay", "Google Pay", "Klarna (Pay in 4)",
                     "Afterpay / Clearpay", "Bank transfer / Wire",
                     "Cryptocurrency", "Buy Now Pay Later"],
         "multi": True},
    42: {"options": ["Suggest bank may have blocked international charge — call bank",
                     "Try a different card or payment method",
                     "Switch to PayPal as a fallback",
                     "Use Klarna's 4-payment split"]},
    43: {"options": ["First-time customer (WELCOME10)",
                     "Abandoned cart recovery (COMEBACK15)",
                     "Complaint resolution (SORRY10)",
                     "Returning customer thank-you (BACK15)",
                     "Holiday / seasonal sales",
                     "Birthday discount"],
         "multi": True},
    44: {"options": ["USD only — single currency",
                     "Charged in customer's local currency (Stripe auto-convert)",
                     "Multi-currency display: USD / EUR / GBP",
                     "Custom regional pricing"]},
    45: {"options": ["VAT included in displayed price (EU)",
                     "Tax added at checkout based on location",
                     "No tax (customer responsible at customs)",
                     "Sales tax shown only for US states where required"]},

    # Loyalty, VIP & After-Sales
    46: {"options": ["No loyalty program",
                     "Points: 1 pt per $1 spent, 100 pts = $5 off",
                     "Tier system (Bronze / Silver / Gold)",
                     "Spend $X to unlock free shipping forever"]},
    47: {"options": ["Free shipping after 3+ orders",
                     "Early access to new launches",
                     "Birthday gift / discount",
                     "Free express upgrade",
                     "Personal account manager (VIP)"],
         "multi": True},
    48: {"options": ["Personal email from the owner",
                     "Priority shipping + handwritten thank-you",
                     "Hand-picked free gift in box",
                     "Dedicated WhatsApp / phone line"]},
    49: {"options": ["Day 2: Thank you for your order",
                     "Day 7: Ask for review (with 10% next-order code)",
                     "Day 14: Reorder reminder",
                     "Day 30: Care tips / how-to-use email",
                     "No automated follow-ups"],
         "multi": True},
    50: {"options": ["No warranty",
                     "30-day defect warranty",
                     "90-day defect warranty",
                     "1-year warranty",
                     "Lifetime warranty (rare — only for premium SKUs)"]},
    51: {"options": ["Yes — free for VIP customers",
                     "Yes — sold separately at cost",
                     "Yes — on request only",
                     "No replacement parts available"]},
    52: {"options": ["Post-purchase email survey",
                     "Automated review request after delivery",
                     "Trustpilot / Google Reviews integration",
                     "In-app rating prompt",
                     "Personal follow-up from owner for repeat customers"],
         "multi": True},
}


# Flatten for lookup — and merge in options/multi flags
QA_BY_ID = {}
for _sec in QA_SECTIONS:
    for _q in _sec["questions"]:
        meta = OPTIONS_MAP.get(_q["id"], {})
        QA_BY_ID[_q["id"]] = {
            **_q,
            "section_id":     _sec["id"],
            "section_name":   _sec["name"],
            "options":        meta.get("options", []),
            "multi":          bool(meta.get("multi", False)),
            "free_text_only": bool(meta.get("free_text_only", False)),
        }

TOTAL_QUESTIONS = len(QA_BY_ID)  # 52


# ─── 10 Advanced Topics ────────────────────────────────────────────────────

ADVANCED_TOPICS = [
    {"key": "Disputes",        "name": "Disputes",            "icon": "fa-gavel",
     "desc": "Handle PayPal / Stripe disputes and customer complaints diplomatically.",
     "qa_section": "disputes_complaints", "color": "danger"},
    {"key": "Address",         "name": "Address Updates",     "icon": "fa-location-dot",
     "desc": "Confirm or reject address change requests before shipping.",
     "qa_section": "address_changes", "color": "info"},
    {"key": "Cancellations",   "name": "Cancellations",       "icon": "fa-ban",
     "desc": "Confirm cancellations within window, refuse if already shipped.",
     "qa_section": "cancellations", "color": "warn"},
    {"key": "Refund",          "name": "Returns & Refunds",   "icon": "fa-rotate-left",
     "desc": "Process returns, communicate refund timeline, handle exceptions.",
     "qa_section": "returns_refunds", "color": "success"},
    {"key": "OutOfStock",      "name": "Out of Stock",        "icon": "fa-box-open",
     "desc": "Notify customers, offer alternatives or refund, set expectations.",
     "qa_section": "products_inventory", "color": "warn"},
    {"key": "ShippingDelays",  "name": "Shipping Delays",     "icon": "fa-truck-fast",
     "desc": "Apologize, set new expectations, offer goodwill discount if delayed long.",
     "qa_section": "shipping", "color": "purple"},
    {"key": "ProductDefects",  "name": "Product Defects",     "icon": "fa-triangle-exclamation",
     "desc": "Apologize, request photos, offer replacement or full refund.",
     "qa_section": "returns_refunds", "color": "danger"},
    {"key": "WrongItem",       "name": "Wrong Item Received", "icon": "fa-shuffle",
     "desc": "Apologize, dispatch correct item urgently, no-return on wrong sends.",
     "qa_section": "returns_refunds", "color": "info"},
    {"key": "PaymentIssues",   "name": "Payment Issues",      "icon": "fa-credit-card",
     "desc": "Failed charges, declined cards, alternative payment options.",
     "qa_section": "payment_pricing", "color": "teal"},
    {"key": "Brand",           "name": "Brand Voice",         "icon": "fa-palette",
     "desc": "Universal tone applied to every reply (uses your sign-off and style).",
     "qa_section": "brand_voice", "color": "purple"},
]

TOPIC_BY_KEY = {t["key"]: t for t in ADVANCED_TOPICS}


# ─── Score helpers ─────────────────────────────────────────────────────────

def _toggles_key_for(topic_key):
    """Convention: 'topic_<lowercase_key>_enabled' lives in profile.toggles."""
    return f"topic_{topic_key.lower()}_enabled"


def get_topic_enabled(profile, topic_key):
    if profile is None:
        return True  # default ON if no profile yet
    toggles = profile.toggles or {}
    return bool(toggles.get(_toggles_key_for(topic_key), True))


def set_topic_enabled(profile, topic_key, enabled):
    toggles = profile.toggles or {}
    toggles[_toggles_key_for(topic_key)] = bool(enabled)
    profile.toggles = toggles
    profile.save(update_fields=["toggles", "updated_at"])


def calculate_topic_score(store, topic_key, profile=None):
    """Confidence score 0–100 for one topic — Q&A-weighted.

    Formula favors Q&A completion (the foundation of training):
      • 14% per related Q&A answer filled (capped at 70%)
      • 12% per active snippet (capped at 30%)

    A tenant who fully completes the relevant Q&A section reaches 70%
    ("Excellent") on its own. Adding 1–3 snippets pushes it to 100%.
    Snippets alone (without Q&A) can only reach 30% — Q&A is essential."""
    if profile is None:
        profile = AiTrainingProfile.objects.filter(store=store).first()

    # Snippet count for this topic
    snip_count = KnowledgeSnippet.objects.filter(
        store=store, category=topic_key, is_enabled=True
    ).count()

    # Q&A answered for the related section
    answers = (profile.wizard_answers or {}) if profile else {}
    topic_meta = TOPIC_BY_KEY.get(topic_key, {})
    section_id = topic_meta.get("qa_section")
    section_def = next((s for s in QA_SECTIONS if s["id"] == section_id), None)
    qa_count = 0
    if section_def:
        for q in section_def["questions"]:
            ans = (answers.get(str(q["id"])) or answers.get(q["id"]) or "").strip()
            if ans:
                qa_count += 1

    qa_score      = min(70, qa_count * 14)
    snippet_score = min(30, snip_count * 12)
    return min(100, qa_score + snippet_score)


def calculate_overall_score(store, profile=None):
    """Average of all enabled topic scores."""
    if profile is None:
        profile = AiTrainingProfile.objects.filter(store=store).first()
    enabled_scores = []
    for t in ADVANCED_TOPICS:
        if get_topic_enabled(profile, t["key"]):
            enabled_scores.append(calculate_topic_score(store, t["key"], profile))
    if not enabled_scores:
        return 0
    return round(sum(enabled_scores) / len(enabled_scores))


def qa_progress(profile):
    """How many of the 52 questions are answered."""
    if not profile:
        return {"answered": 0, "total": TOTAL_QUESTIONS, "pct": 0}
    answers = profile.wizard_answers or {}
    answered = sum(
        1 for q in QA_BY_ID.values()
        if (answers.get(str(q["id"])) or answers.get(q["id"]) or "").strip()
    )
    pct = round((answered / TOTAL_QUESTIONS) * 100) if TOTAL_QUESTIONS else 0
    return {"answered": answered, "total": TOTAL_QUESTIONS, "pct": pct}

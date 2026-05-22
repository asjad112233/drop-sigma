"""
Default Request Snippets — per-category AI training catalog.

Six high-stakes, high-frequency customer-request categories are pinned to the
top of the Knowledge Base. Each has its own dedicated Q&A wizard. Tenant's
answers feed into the AI's system prompt and the auto-reply gate decides
whether to auto-send based on (a) tenant's per-category toggle and (b) a
"maturity score" computed from how many of the questions they answered.

Storage: persisted under AiTrainingProfile.extras['category_training'][slug]:
    {
        "auto_reply_enabled": bool,
        "answers": {qid: value},      # value can be str | list[str] | int
        "maturity_score": int 0..100,
        "completed_count": int,
        "total_weight": float,        # for transparency / re-calc audits
        "updated_at": ISO8601 str,
    }

The gate-check in emails/services.py reads this dict to decide whether a
high-stakes email (refund/cancel/return/etc.) can be auto-sent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Category meta — used for landing cards + keyword routing
# ─────────────────────────────────────────────────────────────────────────────

CATEGORY_META = {
    "refund": {
        "slug": "refund",
        "label": "Refund",
        "icon": "💸",
        "color": "#f59e0b",
        "description": "Refund requests — windows, methods, partial refunds, fees.",
        "keywords": [
            "refund", "money back", "reimburse", "compensation",
            "refund me", "refund my money", "want a refund", "demand a refund",
        ],
    },
    "cancel": {
        "slug": "cancel",
        "label": "Cancel",
        "icon": "❌",
        "color": "#ef4444",
        "description": "Order cancellation — cutoff windows, partial cancels, refund timing.",
        "keywords": [
            "cancel", "cancellation", "cancel my order", "cancel the order",
            "stop my order", "don't ship",
        ],
    },
    "address_change": {
        "slug": "address_change",
        "label": "Address Change",
        "icon": "📍",
        "color": "#06b6d4",
        "description": "Shipping address updates — cutoff timing, verification, intercept fees.",
        "keywords": [
            "change my address", "update my address", "wrong address",
            "different address", "new address", "address update",
            "ship to a different", "update shipping address",
        ],
    },
    "missing_damaged": {
        "slug": "missing_damaged",
        "label": "Missing / Damaged",
        "icon": "📦",
        "color": "#8b5cf6",
        "description": "Lost-in-transit, damaged, wrong item — proof, claims, reship vs refund.",
        "keywords": [
            "missing item", "wrong item", "damaged item", "broken",
            "didn't receive", "did not receive", "never received",
            "package missing", "arrived broken", "item is damaged",
            "wrong product", "not what i ordered",
        ],
    },
    "return": {
        "slug": "return",
        "label": "Return",
        "icon": "↩️",
        "color": "#10b981",
        "description": "Return policy — windows, return shipping, restocking fees, exchanges.",
        "keywords": [
            "return this", "i want to return", "send it back",
            "return policy", "exchange", "swap for", "different size",
        ],
    },
    "dispute": {
        "slug": "dispute",
        "label": "Dispute",
        "icon": "🛡️",
        "color": "#dc2626",
        "description": "Chargebacks, PayPal disputes, formal complaints — evidence, response, escalation.",
        "keywords": [
            "chargeback", "charge back", "dispute the charge", "filed a dispute",
            "paypal dispute", "open a dispute", "bank dispute",
        ],
    },
}

CATEGORY_SLUGS = list(CATEGORY_META.keys())


# ─────────────────────────────────────────────────────────────────────────────
# Question catalogs
# ─────────────────────────────────────────────────────────────────────────────
#
# Question schema:
#   {
#       "id":      "q1",                     # stable id (don't rename!)
#       "type":    "single" | "multi" | "text" | "number",
#       "label":   "Refund window kitne din?",
#       "help":    "Optional hint shown under the label",
#       "options": [                          # for single/multi only
#           {"value": "7_days",  "label": "7 days"},
#           ...
#       ],
#       "weight":  1.0,                       # contribution to maturity score
#       "placeholder": "...",                 # for text/number
#   }
#
# Maturity score = sum(weight for answered questions) / sum(weight all) * 100.
# A "multi" question counts as answered when ≥1 option is selected.
# A "text" question counts as answered when ≥10 non-space chars are entered.

REFUND_QUESTIONS = [
    {"id": "window", "type": "single", "weight": 1.5,
     "label": "Refund eligibility window — order delivery ke kitne din baad tak refund accept karte ho?",
     "options": [
         {"value": "7",   "label": "7 days"},
         {"value": "14",  "label": "14 days"},
         {"value": "30",  "label": "30 days (most common)"},
         {"value": "60",  "label": "60 days"},
         {"value": "90",  "label": "90 days"},
         {"value": "none","label": "No fixed window"},
     ]},
    {"id": "methods", "type": "multi", "weight": 1.2,
     "label": "Refund method options — kis form ma refund de sakte ho?",
     "options": [
         {"value": "original", "label": "Original payment method"},
         {"value": "store_credit", "label": "Store credit"},
         {"value": "bank_transfer", "label": "Bank transfer"},
         {"value": "replacement", "label": "Replacement product"},
     ]},
    {"id": "default_method", "type": "single", "weight": 1.0,
     "label": "Default refund method — pehle kya offer karte ho?",
     "options": [
         {"value": "original", "label": "Original payment (refund to card)"},
         {"value": "store_credit", "label": "Store credit"},
         {"value": "replacement", "label": "Replacement product"},
     ]},
    {"id": "processing_time", "type": "single", "weight": 1.0,
     "label": "Refund processing time (approval ke baad bank ma kitne din lagte hain)?",
     "options": [
         {"value": "instant", "label": "Instant (same day)"},
         {"value": "1_3", "label": "1–3 business days"},
         {"value": "3_5", "label": "3–5 business days"},
         {"value": "5_10", "label": "5–10 business days"},
     ]},
    {"id": "partial_allowed", "type": "single", "weight": 0.8,
     "label": "Partial refunds allow karte ho (e.g., aik damaged item ka refund, baki retain)?",
     "options": [
         {"value": "yes", "label": "Yes — case-by-case"},
         {"value": "no",  "label": "No — full only"},
         {"value": "admin", "label": "Only admin can approve"},
     ]},
    {"id": "shipping_refund", "type": "single", "weight": 0.8,
     "label": "Original shipping fee refund karte ho?",
     "options": [
         {"value": "always", "label": "Always refunded"},
         {"value": "never",  "label": "Non-refundable"},
         {"value": "fault",  "label": "Only if our fault (damaged/wrong item)"},
     ]},
    {"id": "fees", "type": "multi", "weight": 0.7,
     "label": "Kya yeh fees deduct hoti hain refund se?",
     "options": [
         {"value": "restocking",    "label": "Restocking fee"},
         {"value": "payment_gateway", "label": "Payment gateway fee (Stripe/PayPal 2.9%)"},
         {"value": "return_shipping", "label": "Return shipping cost"},
         {"value": "none", "label": "No deductions"},
     ]},
    {"id": "non_refundable", "type": "multi", "weight": 0.9,
     "label": "Kin items pa refund NEVER milta hai?",
     "options": [
         {"value": "custom",     "label": "Custom / personalized items"},
         {"value": "final_sale", "label": "Final-sale / clearance"},
         {"value": "digital",    "label": "Digital products / downloads"},
         {"value": "perishable", "label": "Perishable goods"},
         {"value": "gift_cards", "label": "Gift cards"},
         {"value": "intimate",   "label": "Intimate / hygiene items"},
     ]},
    {"id": "proof_required", "type": "multi", "weight": 1.0,
     "label": "Refund ke liye kya proof maangte ho?",
     "options": [
         {"value": "photos", "label": "Photos of issue"},
         {"value": "video",  "label": "Unboxing video"},
         {"value": "receipt","label": "Order receipt"},
         {"value": "courier","label": "Courier proof"},
         {"value": "none",   "label": "No proof required for trusted customers"},
     ]},
    {"id": "escalate_threshold", "type": "number", "weight": 0.8,
     "label": "Order value > $X ho to AI auto-reply NA kare, human review pa bheje — threshold (USD)?",
     "placeholder": "e.g., 100"},
    {"id": "escalate_cases", "type": "multi", "weight": 0.9,
     "label": "Kin scenarios ma AI khud reply NAHI kare?",
     "options": [
         {"value": "high_value",  "label": "Order > threshold"},
         {"value": "repeat",      "label": "Customer has multiple prior refunds"},
         {"value": "partial",     "label": "Partial refund needed (judgment call)"},
         {"value": "wholesale",   "label": "Wholesale / B2B customer"},
         {"value": "expired",     "label": "Outside refund window"},
     ]},
    {"id": "tone", "type": "single", "weight": 0.6,
     "label": "Refund replies ka tone kya rakhna hai?",
     "options": [
         {"value": "apologetic", "label": "Apologetic + empathetic"},
         {"value": "professional", "label": "Professional + neutral"},
         {"value": "firm", "label": "Firm but polite (policy-driven)"},
     ]},
    {"id": "standard_reply", "type": "text", "weight": 1.2,
     "label": "Standard refund-approved reply — kya likhna hai customer ko?",
     "placeholder": "e.g., \"Hi {name}, your refund of {amount} has been approved and will reflect in your account within 3–5 business days...\""},
    {"id": "refusal_reply", "type": "text", "weight": 1.0,
     "label": "Refund refuse karne ka standard wording (window expired / non-refundable)?",
     "placeholder": "e.g., \"Unfortunately your purchase falls outside our 30-day window, however we'd be happy to offer...\""},
    {"id": "forbidden", "type": "text", "weight": 0.5,
     "label": "AI ko kya NEVER promise nahi karna chahiye? (free text)",
     "placeholder": "e.g., \"Don't promise instant refund — banks take 5-10 days\""},
]

CANCEL_QUESTIONS = [
    {"id": "window", "type": "single", "weight": 1.5,
     "label": "Cancellation window — kis stage tak cancel allow hai?",
     "options": [
         {"value": "1h",  "label": "Within 1 hour of order"},
         {"value": "6h",  "label": "Within 6 hours"},
         {"value": "24h", "label": "Within 24 hours"},
         {"value": "before_fulfillment", "label": "Until fulfillment starts"},
         {"value": "before_ship", "label": "Until shipping label generated"},
         {"value": "anytime", "label": "Anytime before delivery (RTS)"},
     ]},
    {"id": "process", "type": "single", "weight": 1.2,
     "label": "Cancel kaise hota hai?",
     "options": [
         {"value": "auto",   "label": "AI khud Woo/Shopify ma cancel mark kar de"},
         {"value": "team",   "label": "AI acknowledge kare, team manually cancel kare"},
         {"value": "self",   "label": "Customer ko self-cancel link bhejde"},
     ]},
    {"id": "refund_method", "type": "single", "weight": 1.0,
     "label": "Cancellation pa refund kis form ma?",
     "options": [
         {"value": "original", "label": "Original payment method"},
         {"value": "store_credit", "label": "Store credit"},
         {"value": "choice", "label": "Customer ko choice"},
     ]},
    {"id": "refund_timing", "type": "single", "weight": 1.0,
     "label": "Cancellation refund kab process hota hai?",
     "options": [
         {"value": "instant", "label": "Instant"},
         {"value": "24h",     "label": "Within 24 hours"},
         {"value": "3_5",     "label": "3–5 business days"},
         {"value": "7_10",    "label": "7–10 business days"},
     ]},
    {"id": "partial_cancel", "type": "single", "weight": 0.8,
     "label": "Multi-item order ma partial cancellation allow hai?",
     "options": [
         {"value": "yes",   "label": "Yes, allowed"},
         {"value": "no",    "label": "No — full only"},
         {"value": "admin", "label": "Only admin can do partial"},
     ]},
    {"id": "fee", "type": "single", "weight": 0.8,
     "label": "Cancellation fee kuch hai?",
     "options": [
         {"value": "free", "label": "Free (always)"},
         {"value": "free_preship", "label": "Free pre-ship, fee post-ship"},
         {"value": "flat", "label": "Flat fee (specify amount in standard reply)"},
         {"value": "gateway", "label": "Payment gateway fee deducted"},
     ]},
    {"id": "post_ship", "type": "single", "weight": 1.0,
     "label": "Already-shipped order cancel — kya karte ho?",
     "options": [
         {"value": "refuse_delivery", "label": "Customer refuse delivery → refund on return"},
         {"value": "return_process",  "label": "Receive karke return process initiate karein"},
         {"value": "intercept",       "label": "Courier intercept (extra fee on customer)"},
         {"value": "not_allowed",     "label": "Cancellation possible nahi — sirf return policy"},
     ]},
    {"id": "non_cancellable", "type": "multi", "weight": 0.8,
     "label": "Kin items pa cancel NEVER allow hota?",
     "options": [
         {"value": "custom",     "label": "Custom / personalized"},
         {"value": "final_sale", "label": "Final-sale / clearance"},
         {"value": "digital",    "label": "Digital products"},
         {"value": "perishable", "label": "Perishable goods"},
         {"value": "gift_cards", "label": "Gift cards"},
     ]},
    {"id": "supplier_notify", "type": "single", "weight": 0.5,
     "label": "Supplier ko cancellation pa notify karte ho?",
     "options": [
         {"value": "yes", "label": "Yes, every time"},
         {"value": "no",  "label": "No"},
         {"value": "shipped", "label": "Only if already shipped from supplier"},
     ]},
    {"id": "escalate_threshold", "type": "number", "weight": 0.7,
     "label": "Order value > $X — escalate to human (USD)?",
     "placeholder": "e.g., 200"},
    {"id": "escalate_cases", "type": "multi", "weight": 0.8,
     "label": "Kin cases ma AI khud cancel NAHI kare?",
     "options": [
         {"value": "high_value", "label": "Order > threshold"},
         {"value": "repeat",     "label": "Repeat-cancel customer (3rd this month)"},
         {"value": "fulfilled",  "label": "Order > 50% fulfilled"},
         {"value": "bulk",       "label": "Bulk order (qty > 10)"},
         {"value": "wholesale",  "label": "Wholesale / B2B customer"},
         {"value": "discount",   "label": "Expired discount code used"},
     ]},
    {"id": "retention", "type": "single", "weight": 0.5,
     "label": "Retention offer kare? (e.g., 10% discount to keep order)",
     "options": [
         {"value": "always",  "label": "Yes, every cancel"},
         {"value": "threshold", "label": "Only if order > $X"},
         {"value": "never",   "label": "No — direct cancel"},
     ]},
    {"id": "tone", "type": "single", "weight": 0.5,
     "label": "Cancel replies ka tone?",
     "options": [
         {"value": "friendly", "label": "Friendly + accommodating"},
         {"value": "professional", "label": "Professional + neutral"},
         {"value": "retention", "label": "Try-to-retain (with offer)"},
     ]},
    {"id": "standard_reply", "type": "text", "weight": 1.2,
     "label": "Standard cancellation-approved reply?",
     "placeholder": "e.g., \"Hi {name}, your order #{order_id} has been cancelled and a full refund of {amount} is processing...\""},
    {"id": "refusal_reply", "type": "text", "weight": 1.0,
     "label": "Cancellation refused — wording?",
     "placeholder": "e.g., \"Your order has already shipped so we can't cancel — but you can refuse delivery or return for a refund...\""},
    {"id": "forbidden", "type": "text", "weight": 0.5,
     "label": "AI ko NEVER kya nahi kehna?",
     "placeholder": "free text"},
]

ADDRESS_CHANGE_QUESTIONS = [
    {"id": "window", "type": "single", "weight": 1.5,
     "label": "Address change kis stage tak accept karte ho?",
     "options": [
         {"value": "1h",  "label": "Within 1 hour of order"},
         {"value": "12h", "label": "Within 12 hours"},
         {"value": "before_fulfillment", "label": "Until fulfillment starts"},
         {"value": "before_ship", "label": "Until label is generated"},
         {"value": "after_ship", "label": "After ship too — intercept fee applies"},
     ]},
    {"id": "allowed_changes", "type": "multi", "weight": 1.2,
     "label": "Kya kya change allow hai?",
     "options": [
         {"value": "street",     "label": "Street address (same city)"},
         {"value": "city",       "label": "City (same country)"},
         {"value": "state",      "label": "State / Province"},
         {"value": "country",    "label": "Country (risky)"},
         {"value": "recipient",  "label": "Recipient name"},
         {"value": "phone",      "label": "Phone number"},
         {"value": "zip",        "label": "Postal / Zip only"},
     ]},
    {"id": "po_box", "type": "single", "weight": 0.6,
     "label": "PO Box / Military / Remote addresses accept karte ho?",
     "options": [
         {"value": "yes", "label": "Yes, all"},
         {"value": "no",  "label": "No, prohibited"},
         {"value": "case", "label": "Case-by-case (free-text reply)"},
     ]},
    {"id": "fee_policy", "type": "single", "weight": 0.9,
     "label": "Address change ka fee policy?",
     "options": [
         {"value": "free",     "label": "Free (always)"},
         {"value": "preship_free", "label": "Free pre-ship, fee post-ship"},
         {"value": "flat",     "label": "Flat fee always"},
         {"value": "zone",     "label": "Shipping difference charged (zone change)"},
     ]},
    {"id": "intercept_fee", "type": "text", "weight": 0.7,
     "label": "Courier intercept fee (post-ship address change)?",
     "placeholder": "e.g., \"$15 USPS, $20 FedEx, $25 UPS\""},
    {"id": "verification", "type": "multi", "weight": 1.1,
     "label": "Address change confirm karne se pehle kya verify karte ho?",
     "options": [
         {"value": "email_match",  "label": "Original order email match"},
         {"value": "order_name",   "label": "Order number + customer name"},
         {"value": "phone_otp",    "label": "Phone OTP"},
         {"value": "card_last4",   "label": "Last 4 digits of card"},
         {"value": "registered_only", "label": "Reply must come from registered email"},
     ]},
    {"id": "post_ship_action", "type": "single", "weight": 1.0,
     "label": "Already-shipped order ka address change request — default action?",
     "options": [
         {"value": "courier_direct", "label": "Customer khud courier se contact kare"},
         {"value": "intercept", "label": "Hum khud intercept request bhejein (fee customer pe)"},
         {"value": "rts", "label": "Return-to-sender ka wait, phir reship"},
         {"value": "refuse", "label": "Refuse — customer naya order place kare"},
     ]},
    {"id": "escalate_cases", "type": "multi", "weight": 1.0,
     "label": "Kin cases ma AI khud update NA kare, human review pa bheje?",
     "options": [
         {"value": "country",      "label": "Country change request"},
         {"value": "state",        "label": "Different state/province"},
         {"value": "high_value",   "label": "Order > $X threshold"},
         {"value": "repeat",       "label": "2nd+ address change attempt"},
         {"value": "high_risk_product", "label": "High-risk product (electronics, jewelry)"},
         {"value": "domain_mismatch", "label": "Email/phone country mismatch with new address"},
         {"value": "freight_forwarder", "label": "Freight forwarder address (Shipito, MyUS)"},
     ]},
    {"id": "high_value_threshold", "type": "number", "weight": 0.6,
     "label": "High-value threshold (USD)?",
     "placeholder": "e.g., 150"},
    {"id": "freight_forwarder", "type": "single", "weight": 0.5,
     "label": "Freight forwarder addresses (Shipito, MyUS, etc.) accept?",
     "options": [
         {"value": "yes", "label": "Yes, allowed"},
         {"value": "no",  "label": "No, prohibited"},
         {"value": "case","label": "Case-by-case review"},
     ]},
    {"id": "update_process", "type": "single", "weight": 0.8,
     "label": "Address update kaise execute hota hai?",
     "options": [
         {"value": "ai_direct",    "label": "AI khud Woo/Shopify ma update kare"},
         {"value": "team_review",  "label": "AI confirm kare, team manually update kare"},
         {"value": "ack_only",     "label": "AI sirf acknowledge kare — human review"},
     ]},
    {"id": "confirmation_channel", "type": "multi", "weight": 0.5,
     "label": "Customer ko confirmation kis form ma deni hai?",
     "options": [
         {"value": "email", "label": "Email"},
         {"value": "sms",   "label": "SMS"},
         {"value": "both",  "label": "Both"},
     ]},
    {"id": "confirmation_include", "type": "multi", "weight": 0.6,
     "label": "Confirmation ma kya include karna hai?",
     "options": [
         {"value": "echo_address", "label": "Full new address echo back"},
         {"value": "eta",          "label": "Updated delivery ETA"},
         {"value": "tracking",     "label": "Tracking number (if generated)"},
     ]},
    {"id": "standard_reply_preship", "type": "text", "weight": 1.0,
     "label": "Pre-ship valid address change — standard reply?",
     "placeholder": "e.g., \"Hi {name}, your shipping address has been updated to {new_address}. Your order will be shipped to the new address...\""},
    {"id": "standard_reply_postship", "type": "text", "weight": 1.0,
     "label": "Post-ship address change — standard reply?",
     "placeholder": "e.g., \"Your order has already shipped. We can attempt an intercept for $15...\""},
    {"id": "refusal_reply", "type": "text", "weight": 0.8,
     "label": "Cutoff window guzar gaya — refusal wording?",
     "placeholder": "free text"},
    {"id": "forbidden", "type": "text", "weight": 0.5,
     "label": "AI ko NEVER kya promise nahi karna chahiye?",
     "placeholder": "e.g., \"Don't promise delivery date will stay the same after address change\""},
]

MISSING_DAMAGED_QUESTIONS = [
    {"id": "proof_missing", "type": "multi", "weight": 1.3,
     "label": "Missing parcel — kya proof maangte ho customer se?",
     "options": [
         {"value": "wait_days",    "label": "Wait X days after expected delivery first"},
         {"value": "neighbors",    "label": "Check with neighbors / building"},
         {"value": "courier_claim","label": "File courier claim first"},
         {"value": "police_report","label": "Police report (high-value)"},
         {"value": "none",         "label": "No proof — trust customer"},
     ]},
    {"id": "wait_days", "type": "number", "weight": 0.7,
     "label": "Expected delivery ke kitne din baad missing claim accept karte ho?",
     "placeholder": "e.g., 5"},
    {"id": "missing_action", "type": "single", "weight": 1.2,
     "label": "Confirmed missing — default action?",
     "options": [
         {"value": "reship", "label": "Reship same item free"},
         {"value": "refund", "label": "Full refund"},
         {"value": "choice", "label": "Customer ko choice (reship / refund)"},
         {"value": "case",   "label": "Case-by-case (admin reviews)"},
     ]},
    {"id": "proof_damaged", "type": "multi", "weight": 1.3,
     "label": "Damaged item — kya proof maangte ho?",
     "options": [
         {"value": "photos",        "label": "Photos of damage"},
         {"value": "video",         "label": "Unboxing video"},
         {"value": "packaging",     "label": "Photo of damaged packaging"},
         {"value": "all_angles",    "label": "Photos from all angles"},
         {"value": "courier_label", "label": "Photo of shipping label"},
     ]},
    {"id": "damaged_action", "type": "single", "weight": 1.2,
     "label": "Confirmed damaged — default action?",
     "options": [
         {"value": "replace",       "label": "Replacement at no cost"},
         {"value": "refund_partial","label": "Partial refund + keep item"},
         {"value": "refund_full",   "label": "Full refund + return required"},
         {"value": "choice",        "label": "Customer ko choice"},
     ]},
    {"id": "wrong_item_action", "type": "single", "weight": 1.0,
     "label": "Wrong item shipped — default action?",
     "options": [
         {"value": "send_correct", "label": "Send correct + customer keeps wrong (small items)"},
         {"value": "send_correct_return","label": "Send correct + return wrong (with label)"},
         {"value": "refund_only",   "label": "Refund only — customer reorders correct"},
         {"value": "case",          "label": "Case-by-case"},
     ]},
    {"id": "deadline", "type": "single", "weight": 1.0,
     "label": "Customer ko claim file karne ka window (delivery ke baad)?",
     "options": [
         {"value": "48h",  "label": "48 hours"},
         {"value": "7d",   "label": "7 days"},
         {"value": "14d",  "label": "14 days"},
         {"value": "30d",  "label": "30 days"},
         {"value": "none", "label": "No deadline"},
     ]},
    {"id": "return_required", "type": "single", "weight": 0.8,
     "label": "Damaged/wrong item — return zaruri hai before refund?",
     "options": [
         {"value": "yes", "label": "Yes, always"},
         {"value": "value", "label": "Only if value > $X"},
         {"value": "no",  "label": "No — disposal at customer side"},
     ]},
    {"id": "return_shipping_cost", "type": "single", "weight": 0.8,
     "label": "Damaged item ka return shipping kon bharta hai?",
     "options": [
         {"value": "us",     "label": "Hum bharte hain (provide label)"},
         {"value": "customer","label": "Customer initially pays, hum reimburse karte"},
         {"value": "no_return","label": "Return required nahi"},
     ]},
    {"id": "reship_cost", "type": "single", "weight": 0.6,
     "label": "Reship case ma shipping cost?",
     "options": [
         {"value": "free",   "label": "Free reship"},
         {"value": "expedited", "label": "Free + expedited shipping"},
         {"value": "customer", "label": "Customer pays shipping again"},
     ]},
    {"id": "escalate_threshold", "type": "number", "weight": 0.7,
     "label": "Item value > $X — escalate to human (USD)?",
     "placeholder": "e.g., 100"},
    {"id": "escalate_cases", "type": "multi", "weight": 1.0,
     "label": "Kin cases ma AI khud reply NA kare?",
     "options": [
         {"value": "high_value",   "label": "Item > threshold"},
         {"value": "repeat",       "label": "Customer's 2nd+ missing/damaged claim"},
         {"value": "no_proof",     "label": "Customer refuses to provide proof"},
         {"value": "delivered_status","label": "Tracking shows 'delivered' (POD dispute)"},
         {"value": "high_value_product","label": "High-risk product (jewelry, electronics)"},
     ]},
    {"id": "supplier_recovery", "type": "single", "weight": 0.5,
     "label": "Supplier se cost recover karte ho?",
     "options": [
         {"value": "always", "label": "Always — file claim with supplier"},
         {"value": "case",   "label": "Case-by-case"},
         {"value": "never",  "label": "No, absorb the loss"},
     ]},
    {"id": "tone", "type": "single", "weight": 0.5,
     "label": "Replies ka tone?",
     "options": [
         {"value": "apologetic",  "label": "Highly apologetic + urgent"},
         {"value": "professional","label": "Professional + neutral"},
         {"value": "investigative","label": "Investigative (ask for details first)"},
     ]},
    {"id": "missing_reply", "type": "text", "weight": 1.2,
     "label": "\"Item nahi mila\" — aap ka standard reply?",
     "placeholder": "e.g., \"Hi {name}, sorry to hear your package hasn't arrived. Tracking shows {status}. We're reshipping at no cost...\""},
    {"id": "damaged_reply", "type": "text", "weight": 1.2,
     "label": "\"Item damaged mila\" — aap ka standard reply?",
     "placeholder": "e.g., \"Hi {name}, very sorry for the damaged item. Please send 2-3 photos of the damage...\""},
    {"id": "wrong_item_reply", "type": "text", "weight": 1.0,
     "label": "\"Wrong item / different item\" — standard reply?",
     "placeholder": "free text"},
    {"id": "forbidden", "type": "text", "weight": 0.5,
     "label": "AI ko NEVER kya nahi kehna?",
     "placeholder": "e.g., \"Don't admit our packaging was faulty without proof — supplier may dispute\""},
]

RETURN_QUESTIONS = [
    {"id": "window", "type": "single", "weight": 1.5,
     "label": "Return window — delivery ke kitne din baad tak return accept karte ho?",
     "options": [
         {"value": "7",   "label": "7 days"},
         {"value": "14",  "label": "14 days"},
         {"value": "30",  "label": "30 days (most common)"},
         {"value": "60",  "label": "60 days"},
         {"value": "90",  "label": "90 days"},
         {"value": "none","label": "No fixed window"},
     ]},
    {"id": "condition", "type": "multi", "weight": 1.1,
     "label": "Return acceptance ki shartein?",
     "options": [
         {"value": "unused",   "label": "Item unused / unopened"},
         {"value": "original_packaging","label": "Original packaging required"},
         {"value": "tags_attached","label": "Tags attached"},
         {"value": "receipt",  "label": "Receipt / order proof"},
         {"value": "any_condition","label": "Any condition (lenient)"},
     ]},
    {"id": "shipping_cost", "type": "single", "weight": 1.2,
     "label": "Return shipping cost kon bharta hai?",
     "options": [
         {"value": "us",       "label": "Hum bharte hain (free returns)"},
         {"value": "customer", "label": "Customer pays"},
         {"value": "fault",    "label": "Hum agar our fault, customer agar buyer's remorse"},
         {"value": "deduct",   "label": "Deducted from refund"},
     ]},
    {"id": "restocking_fee", "type": "single", "weight": 0.8,
     "label": "Restocking fee?",
     "options": [
         {"value": "none",   "label": "No fee"},
         {"value": "10pct",  "label": "10% of order value"},
         {"value": "15pct",  "label": "15% of order value"},
         {"value": "20pct",  "label": "20% of order value"},
         {"value": "flat",   "label": "Flat fee per item"},
     ]},
    {"id": "exchange_allowed", "type": "single", "weight": 0.8,
     "label": "Exchange (size/color swap) allow karte ho?",
     "options": [
         {"value": "yes_free",  "label": "Yes — free exchange (we pay shipping both ways)"},
         {"value": "yes_paid",  "label": "Yes — customer pays return shipping"},
         {"value": "no",        "label": "No exchanges — return + reorder"},
     ]},
    {"id": "rma_required", "type": "single", "weight": 0.7,
     "label": "Return authorization (RMA) zaruri hai?",
     "options": [
         {"value": "yes", "label": "Yes — RMA number needed before shipping back"},
         {"value": "no",  "label": "No — customer can return directly"},
     ]},
    {"id": "label_provided", "type": "single", "weight": 0.7,
     "label": "Return label aap provide karte ho?",
     "options": [
         {"value": "yes_prepaid","label": "Yes — prepaid label emailed"},
         {"value": "yes_paid",   "label": "Yes — but cost deducted from refund"},
         {"value": "no",         "label": "No — customer arranges own shipping"},
     ]},
    {"id": "non_returnable", "type": "multi", "weight": 1.0,
     "label": "Kin items pa return NEVER milta?",
     "options": [
         {"value": "custom",     "label": "Custom / personalized"},
         {"value": "final_sale", "label": "Final-sale / clearance"},
         {"value": "digital",    "label": "Digital products"},
         {"value": "perishable", "label": "Perishable goods"},
         {"value": "intimate",   "label": "Intimate / hygiene items"},
         {"value": "gift_cards", "label": "Gift cards"},
         {"value": "swimwear",   "label": "Swimwear / underwear"},
     ]},
    {"id": "refund_timing", "type": "single", "weight": 0.9,
     "label": "Return receive karne ke baad refund kab process hota?",
     "options": [
         {"value": "instant",      "label": "Instant on receipt"},
         {"value": "24_48h",       "label": "24–48 hours after inspection"},
         {"value": "3_5",          "label": "3–5 business days"},
         {"value": "7_10",         "label": "7–10 business days"},
     ]},
    {"id": "refund_method", "type": "single", "weight": 0.8,
     "label": "Return ka refund kis form ma?",
     "options": [
         {"value": "original",    "label": "Original payment method"},
         {"value": "store_credit","label": "Store credit only"},
         {"value": "choice",      "label": "Customer ko choice"},
     ]},
    {"id": "international", "type": "single", "weight": 0.7,
     "label": "International returns kaise handle?",
     "options": [
         {"value": "accept", "label": "Accept — customer pays return shipping"},
         {"value": "no",     "label": "No international returns"},
         {"value": "case",   "label": "Case-by-case (high-value items only)"},
     ]},
    {"id": "escalate_threshold", "type": "number", "weight": 0.6,
     "label": "Return value > $X — escalate to human (USD)?",
     "placeholder": "e.g., 200"},
    {"id": "escalate_cases", "type": "multi", "weight": 0.9,
     "label": "Kin cases ma AI khud return approve NA kare?",
     "options": [
         {"value": "high_value",   "label": "Return > threshold"},
         {"value": "repeat",       "label": "Customer's 3rd+ return this year"},
         {"value": "outside_window","label": "Outside return window"},
         {"value": "non_returnable","label": "Non-returnable item"},
         {"value": "international","label": "International return"},
     ]},
    {"id": "tone", "type": "single", "weight": 0.5,
     "label": "Return reply tone?",
     "options": [
         {"value": "accommodating","label": "Accommodating + helpful"},
         {"value": "professional", "label": "Professional + neutral"},
         {"value": "policy_firm",  "label": "Policy-firm (no exceptions)"},
     ]},
    {"id": "approval_reply", "type": "text", "weight": 1.2,
     "label": "Return approved — standard reply?",
     "placeholder": "e.g., \"Hi {name}, your return is approved. Please ship the item back to {address} using this prepaid label...\""},
    {"id": "refusal_reply", "type": "text", "weight": 1.0,
     "label": "Return refused (window/condition) — wording?",
     "placeholder": "free text"},
    {"id": "forbidden", "type": "text", "weight": 0.5,
     "label": "AI ko NEVER kya nahi kehna?",
     "placeholder": "free text"},
]

DISPUTE_QUESTIONS = [
    {"id": "stance", "type": "single", "weight": 1.5,
     "label": "Chargeback / dispute file hone pa default stance?",
     "options": [
         {"value": "refund_first", "label": "Foran refund de dete hain (fight nahi karte)"},
         {"value": "fight",        "label": "Evidence ikatha karke fight karte hain"},
         {"value": "case",         "label": "Case-by-case decide karte hain"},
     ]},
    {"id": "pre_dispute_offer", "type": "single", "weight": 1.0,
     "label": "Customer dispute threaten kare to pehle kya offer karte ho?",
     "options": [
         {"value": "full_refund",     "label": "Full refund immediately"},
         {"value": "replacement",     "label": "Replacement first"},
         {"value": "partial_or_credit","label": "Partial refund / store credit"},
         {"value": "investigate",     "label": "Investigate first, then decide"},
     ]},
    {"id": "alternatives", "type": "multi", "weight": 1.0,
     "label": "Dispute resolution ma kya offer karte ho?",
     "options": [
         {"value": "full_refund",   "label": "Full refund"},
         {"value": "partial_refund","label": "Partial refund"},
         {"value": "store_credit",  "label": "Store credit"},
         {"value": "replacement",   "label": "Replacement product"},
         {"value": "free_reship",   "label": "Free reship"},
     ]},
    {"id": "evidence", "type": "multi", "weight": 1.2,
     "label": "Customer se kya evidence maangte ho?",
     "options": [
         {"value": "damage_photos", "label": "Photos of damaged item"},
         {"value": "tracking",      "label": "Tracking screenshot"},
         {"value": "unboxing_video","label": "Unboxing video"},
         {"value": "receipt",       "label": "Order receipt"},
         {"value": "id_verify",     "label": "ID verification (fraud cases)"},
     ]},
    {"id": "deadline", "type": "single", "weight": 0.9,
     "label": "Evidence submit karne ki deadline?",
     "options": [
         {"value": "48h", "label": "48 hours"},
         {"value": "7d",  "label": "7 days"},
         {"value": "14d", "label": "14 days"},
         {"value": "30d", "label": "30 days"},
     ]},
    {"id": "window", "type": "single", "weight": 0.9,
     "label": "Order delivery ke kitne din baad tak dispute accept karte ho?",
     "options": [
         {"value": "30d", "label": "30 days"},
         {"value": "60d", "label": "60 days"},
         {"value": "90d", "label": "90 days"},
         {"value": "180d","label": "180 days"},
         {"value": "none","label": "No fixed window"},
     ]},
    {"id": "response_time", "type": "single", "weight": 1.0,
     "label": "Customer ko response time kya promise karte ho?",
     "options": [
         {"value": "24h",  "label": "Within 24 hours"},
         {"value": "48h",  "label": "Within 48 hours"},
         {"value": "2_3d", "label": "2–3 business days"},
         {"value": "5d",   "label": "Within 5 business days"},
     ]},
    {"id": "channels", "type": "multi", "weight": 0.7,
     "label": "Aap kin channels pa disputes handle karte ho?",
     "options": [
         {"value": "paypal", "label": "PayPal Resolution Center"},
         {"value": "stripe", "label": "Stripe Dispute Dashboard"},
         {"value": "bank",   "label": "Bank chargeback"},
         {"value": "email",  "label": "Email-only"},
         {"value": "phone",  "label": "WhatsApp / Phone"},
     ]},
    {"id": "tone", "type": "single", "weight": 0.8,
     "label": "Initial reply ka tone?",
     "options": [
         {"value": "deescalate", "label": "Apologetic + de-escalating"},
         {"value": "firm",       "label": "Firm but polite (policy-driven)"},
         {"value": "professional","label": "Professional / neutral"},
     ]},
    {"id": "ask_lift", "type": "single", "weight": 0.6,
     "label": "Customer ko likhna chahiye \"dispute lift karne ki request karein\"?",
     "options": [
         {"value": "yes", "label": "Yes — after refund processed"},
         {"value": "no",  "label": "No — bank/processor handle karta hai"},
     ]},
    {"id": "escalate_threshold", "type": "number", "weight": 0.8,
     "label": "Amount > $X — AI khud reply NA kare (USD)?",
     "placeholder": "e.g., 75"},
    {"id": "escalate_cases", "type": "multi", "weight": 1.2,
     "label": "Kin cases ma AI khud reply NAHI kare?",
     "options": [
         {"value": "high_value",  "label": "Amount > threshold"},
         {"value": "repeat",      "label": "Repeat disputer (prior cases)"},
         {"value": "legal",       "label": "Legal language used (lawyer, small claims)"},
         {"value": "bank",        "label": "Bank chargeback (not PayPal)"},
         {"value": "fraud_flag",  "label": "Suspected fraud / chargeback abuse"},
     ]},
    {"id": "repeat_disputer", "type": "single", "weight": 0.7,
     "label": "Repeat disputer mile to kya karein?",
     "options": [
         {"value": "block",   "label": "Block / blacklist account"},
         {"value": "refuse",  "label": "Refuse future orders"},
         {"value": "escalate","label": "Escalate to admin only"},
         {"value": "case",    "label": "Case-by-case"},
     ]},
    {"id": "authority", "type": "text", "weight": 0.6,
     "label": "Dispute resolve karne ka final authority kis ka hai? (team email / name)",
     "placeholder": "e.g., \"disputes@brand.com — handled by Sara\""},
    {"id": "refund_timeline", "type": "text", "weight": 0.7,
     "label": "Customer ko refund ka exact timeline kya batate ho?",
     "placeholder": "e.g., \"3–5 business days after approval\""},
    {"id": "missing_reply", "type": "text", "weight": 1.0,
     "label": "\"Item nahi mila\" (lost in transit) — standard reply?",
     "placeholder": "free text"},
    {"id": "damaged_reply", "type": "text", "weight": 1.0,
     "label": "\"Item damaged mila\" — standard reply?",
     "placeholder": "free text"},
    {"id": "not_as_desc", "type": "text", "weight": 0.8,
     "label": "\"Item like description nahi\" — policy / reply?",
     "placeholder": "free text"},
    {"id": "forbidden", "type": "text", "weight": 0.5,
     "label": "AI ko NEVER kya nahi kehna?",
     "placeholder": "e.g., \"Don't promise instant refund\", \"Don't admit fault\", \"Don't say 'guaranteed'\""},
]


CATEGORY_QUESTIONS = {
    "refund":          REFUND_QUESTIONS,
    "cancel":          CANCEL_QUESTIONS,
    "address_change":  ADDRESS_CHANGE_QUESTIONS,
    "missing_damaged": MISSING_DAMAGED_QUESTIONS,
    "return":          RETURN_QUESTIONS,
    "dispute":         DISPUTE_QUESTIONS,
}


# ─────────────────────────────────────────────────────────────────────────────
# Maturity scoring
# ─────────────────────────────────────────────────────────────────────────────

# When maturity ≥ this threshold AND the tenant's per-category toggle is ON,
# the gate-check unlocks auto-send for that category. Below this, even if the
# toggle is on, replies stay in draft (we won't auto-send under-trained AI).
MIN_MATURITY_FOR_AUTO_SEND = 60


def _question_answered(q: dict, value: Any) -> bool:
    """Treat a question as 'answered' when its value is meaningful."""
    if value is None or value == "":
        return False
    qtype = q.get("type", "single")
    if qtype == "multi":
        return isinstance(value, (list, tuple)) and len(value) > 0
    if qtype == "text":
        try:
            return len(str(value).strip()) >= 10
        except Exception:
            return False
    if qtype == "number":
        try:
            return float(value) >= 0
        except (TypeError, ValueError):
            return False
    # single
    return bool(str(value).strip())


def compute_maturity(slug: str, answers: dict | None) -> dict:
    """Returns {score: 0..100, completed: N, total: M, total_weight: W}."""
    questions = CATEGORY_QUESTIONS.get(slug) or []
    answers = answers or {}
    total_weight = sum(q.get("weight", 1.0) for q in questions)
    earned = 0.0
    completed = 0
    for q in questions:
        if _question_answered(q, answers.get(q["id"])):
            earned += q.get("weight", 1.0)
            completed += 1
    score = round((earned / total_weight) * 100) if total_weight else 0
    return {
        "score": max(0, min(100, int(score))),
        "completed": completed,
        "total": len(questions),
        "total_weight": round(total_weight, 2),
    }


def get_category_state(profile, slug: str) -> dict:
    """Read the per-category state from profile.extras (with safe defaults)."""
    if slug not in CATEGORY_META:
        return {}
    extras = (profile.extras or {}) if profile else {}
    cat_map = extras.get("category_training") or {}
    saved = cat_map.get(slug) or {}
    answers = saved.get("answers") or {}
    maturity = compute_maturity(slug, answers)
    return {
        "slug": slug,
        "auto_reply_enabled": bool(saved.get("auto_reply_enabled", False)),
        "answers": answers,
        "maturity_score": maturity["score"],
        "completed_count": maturity["completed"],
        "total_count": maturity["total"],
        "updated_at": saved.get("updated_at"),
    }


def save_category_state(profile, slug: str, *, answers=None,
                        auto_reply_enabled=None) -> dict:
    """Update profile.extras['category_training'][slug] and recompute score."""
    if slug not in CATEGORY_META:
        raise ValueError(f"Unknown category: {slug}")
    extras = dict(profile.extras or {})
    cat_map = dict(extras.get("category_training") or {})
    saved = dict(cat_map.get(slug) or {})

    if answers is not None:
        # Strip unknown question ids to keep storage clean
        valid_ids = {q["id"] for q in CATEGORY_QUESTIONS.get(slug, [])}
        saved["answers"] = {k: v for k, v in (answers or {}).items() if k in valid_ids}

    if auto_reply_enabled is not None:
        saved["auto_reply_enabled"] = bool(auto_reply_enabled)

    maturity = compute_maturity(slug, saved.get("answers") or {})
    saved["maturity_score"] = maturity["score"]
    saved["completed_count"] = maturity["completed"]
    saved["total_count"] = maturity["total"]
    saved["updated_at"] = datetime.now(timezone.utc).isoformat()

    cat_map[slug] = saved
    extras["category_training"] = cat_map
    profile.extras = extras
    profile.save(update_fields=["extras", "updated_at"])
    return get_category_state(profile, slug)


def list_all_states(profile) -> list[dict]:
    """Return state for every category — used by the landing API."""
    return [
        {
            **get_category_state(profile, slug),
            "label":       CATEGORY_META[slug]["label"],
            "icon":        CATEGORY_META[slug]["icon"],
            "color":       CATEGORY_META[slug]["color"],
            "description": CATEGORY_META[slug]["description"],
        }
        for slug in CATEGORY_SLUGS
    ]


def build_prompt_block(profile, body: str, subject: str = "") -> str:
    """Build a prompt block describing the tenant's trained policy for the
    detected category. Returns an empty string when the email doesn't match
    any category or the tenant hasn't answered anything yet.

    The block is injected into the AI system prompt so replies follow the
    exact policy (refund window, return shipping, dispute stance, etc.).
    """
    if not profile:
        return ""
    text = " ".join([subject or "", body or ""])
    slug = detect_category(text.lower())
    if not slug:
        return ""
    state = get_category_state(profile, slug)
    answers = state.get("answers") or {}
    if not answers:
        return ""
    meta = CATEGORY_META[slug]
    questions = {q["id"]: q for q in CATEGORY_QUESTIONS.get(slug, [])}

    def _human(qid: str, value: Any) -> str:
        q = questions.get(qid)
        if not q:
            return str(value)
        if q.get("type") == "multi" and isinstance(value, (list, tuple)):
            opt_lookup = {o["value"]: o["label"] for o in q.get("options", [])}
            return ", ".join(opt_lookup.get(v, v) for v in value) or "—"
        if q.get("type") in ("single",):
            opt_lookup = {o["value"]: o["label"] for o in q.get("options", [])}
            return opt_lookup.get(str(value), str(value))
        return str(value)

    lines = [
        f"\n\n🎯 CATEGORY POLICY — {meta['label'].upper()} "
        f"(maturity {state.get('maturity_score', 0)}%):"
    ]
    for q in CATEGORY_QUESTIONS.get(slug, []):
        v = answers.get(q["id"])
        if v in (None, "", [], {}):
            continue
        lines.append(f"• {q['label']}\n    → {_human(q['id'], v)}")
    lines.append(
        "Follow this policy exactly. Do NOT promise anything beyond these limits. "
        "If the customer's request falls outside the trained answers, say so and "
        "flag for human review."
    )
    return "\n".join(lines)


def detect_category(body: str) -> str | None:
    """Map an email body to a category slug by keyword hit.

    Used by the auto-reply gate so we can look up the tenant's per-category
    toggle + maturity score. Returns the first matching slug (priority order
    is the iteration order of CATEGORY_META).
    """
    if not body:
        return None
    text = body.lower()
    for slug, meta in CATEGORY_META.items():
        for kw in meta.get("keywords") or []:
            if kw in text:
                return slug
    return None

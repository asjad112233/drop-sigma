import re
import imaplib
import email
import os
import requests as _requests

import anthropic
from email.header import decode_header
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from stores.models import Store
from orders.models import Order
from .models import EmailMessage, EmailAccount, EmailAttachment


# =========================
# 🤖 CLAUDE (ANTHROPIC) CONFIG
# =========================
_claude_client = None

def get_claude_client():
    global _claude_client
    if _claude_client is None:
        _claude_client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _claude_client


# =========================
# 🔍 CUSTOMER REFERENCE EXTRACTION + LOOKUP
# =========================
def extract_customer_refs(text):
    """
    Scan free-text (email body, subject) for customer identifiers.
    Returns list of dicts: [{type, value}, ...]
    Types: 'order_id', 'email', 'phone', 'tracking'
    """
    text = text or ""
    refs = []
    seen = set()

    def _add(t, v):
        v = (v or "").strip()
        if not v:
            return
        key = f"{t}:{v.lower()}"
        if key in seen:
            return
        seen.add(key)
        refs.append({"type": t, "value": v})

    # Order numbers — broad patterns
    # "#1234", "order 1234", "order # 1234", "order number 1234", "order id 1234"
    for m in re.finditer(r"(?:order\s*(?:#|no\.?|number|id)?\s*[:#]?\s*)(\d{3,10})\b", text, re.IGNORECASE):
        _add("order_id", m.group(1))
    for m in re.finditer(r"#(\d{3,10})\b", text):
        _add("order_id", m.group(1))

    # Email addresses
    for m in re.finditer(r"\b[\w.+\-]+@[\w-]+\.[\w.\-]+\b", text):
        _add("email", m.group(0))

    # Phone numbers (loose — 7+ digits, may have +, spaces, dashes, parens)
    for m in re.finditer(r"\+?\d[\d\s\-().]{7,}\d", text):
        digits = re.sub(r"\D", "", m.group(0))
        if 7 <= len(digits) <= 15:
            _add("phone", m.group(0).strip())

    # Tracking numbers — usually preceded by "tracking" keyword
    for m in re.finditer(r"tracking\s*(?:#|no\.?|number|id)?\s*[:#]?\s*([A-Z0-9]{8,30})\b", text, re.IGNORECASE):
        _add("tracking", m.group(1))

    return refs


def resolve_customer_context(refs, sender_email=None, store=None):
    """
    Take extracted refs + optional sender email, look up matching Orders.
    Each order is annotated with a `_verified_sender` attribute:
      True  → the order's customer_email matches the sender email (trusted)
      False → no match (DO NOT share details without further verification)
    Returns dict {orders: [...], notes: [...], sender_email_clean: str}
    """
    found_orders = []
    notes = []
    seen_order_ids = set()

    # Clean the sender email up-front for matching
    sender_clean = ""
    if sender_email:
        m = re.search(r"<([^>]+)>", sender_email)
        sender_clean = (m.group(1) if m else sender_email).strip().lower()

    def _add_order(o):
        if o.id in seen_order_ids:
            return
        seen_order_ids.add(o.id)
        # Annotate verification status
        order_email = (o.customer_email or "").strip().lower()
        o._verified_sender = bool(sender_clean and order_email and sender_clean == order_email)
        found_orders.append(o)

    qs_base = Order.objects.all()
    if store is not None:
        qs_base = qs_base.filter(store=store)

    # Lookup by extracted refs
    for ref in (refs or []):
        t, v = ref.get("type"), ref.get("value")
        if not v:
            continue
        try:
            if t == "order_id":
                for o in qs_base.filter(external_order_id__iendswith=v)[:3]:
                    _add_order(o)
            elif t == "email":
                for o in qs_base.filter(customer_email__iexact=v).order_by("-created_at")[:3]:
                    _add_order(o)
            elif t == "phone":
                digits = re.sub(r"\D", "", v)
                if digits:
                    for o in qs_base.filter(customer_phone__contains=digits[-7:]).order_by("-created_at")[:3]:
                        _add_order(o)
            elif t == "tracking":
                for o in qs_base.filter(tracking_number__iexact=v)[:3]:
                    _add_order(o)
        except Exception as e:
            notes.append(f"lookup error for {t}={v}: {e}")

    # Also try sender email as a customer email match — these are inherently VERIFIED
    if sender_clean and "@" in sender_clean:
        try:
            for o in qs_base.filter(customer_email__iexact=sender_clean).order_by("-created_at")[:3]:
                _add_order(o)
        except Exception as e:
            notes.append(f"sender lookup error: {e}")

    return {"orders": found_orders[:5], "notes": notes, "sender_email_clean": sender_clean}


def serialize_order_for_ai(o):
    """Compact representation of an Order for AI prompt context + UI display."""
    from datetime import datetime
    from django.utils import timezone as _tz

    # Days since the order was placed (for ETA reasoning)
    days_since_order = None
    if getattr(o, "created_at", None):
        try:
            days_since_order = (_tz.now() - o.created_at).days
        except Exception:
            days_since_order = None

    return {
        "id": o.id,
        "order_number": o.external_order_id,
        "customer_name": o.customer_name or "",
        "customer_email": o.customer_email or "",
        "city": o.city or "",
        "country": o.country or "",
        "product": o.product_name or "",
        "total": f"{o.total_price} {o.currency or ''}".strip(),
        "payment_status": o.payment_status or "",
        "fulfillment_status": o.fulfillment_status or "",
        "tracking_status": o.tracking_status or "",
        "live_tracking_status": o.live_tracking_status or "",
        "tracking_number": o.tracking_number or "",
        "tracking_company": o.tracking_company or "",
        "tracking_url": o.tracking_url or "",
        "delivered_at": o.delivered_at.isoformat() if o.delivered_at else "",
        "created_at": o.created_at.isoformat() if o.created_at else "",
        "days_since_order": days_since_order,
        # ⚠️ Security flag — only show details for verified orders
        "verified_sender": bool(getattr(o, "_verified_sender", False)),
    }


# ─── Status code → plain-English translator ──────────────────────────────────
# WooCommerce + Shopify use compact status codes that the AI must interpret
# CORRECTLY. The biggest pitfall: "processing" in WooCommerce means "payment
# confirmed, order being fulfilled" — NOT "payment is pending verification".
# Without this translation the AI sometimes tells customers their payment is
# still being verified when it's actually already paid.

_PAYMENT_STATUS_MEANINGS = {
    # WooCommerce
    "processing":    "PAID — payment received, order is being prepared for shipment",
    "completed":     "PAID and shipped — order fulfilled",
    "on-hold":       "Payment NOT yet received — awaiting manual confirmation",
    "pending":       "Payment NOT yet received — order placed but payment still pending",
    "pending-payment":"Payment NOT yet received — order placed but payment still pending",
    "failed":        "Payment FAILED — order cannot proceed without retry",
    "cancelled":     "CANCELLED",
    "refunded":      "REFUNDED",
    # Shopify-style
    "paid":          "PAID — payment captured in full",
    "partially_paid":"Partial payment captured",
    "authorized":    "Payment authorized but not yet captured",
    "partially_refunded":"Partially refunded",
    "voided":        "Payment voided",
}

_FULFILLMENT_STATUS_MEANINGS = {
    # WooCommerce uses the same labels as payment in many setups
    "processing":    "Order is being prepared for shipment (not yet shipped)",
    "completed":     "Shipped",
    "shipped":       "Shipped",
    "on-hold":       "Fulfillment paused",
    "pending":       "Awaiting fulfillment to start",
    "cancelled":     "Cancelled — will not ship",
    "refunded":      "Refunded — will not ship",
    # Shopify
    "fulfilled":     "Shipped",
    "partial":       "Partially shipped",
    "unfulfilled":   "Not yet shipped",
    "restocked":     "Restocked",
}

def _humanize_status(value, mapping):
    if not value:
        return ""
    key = str(value).strip().lower().replace(" ", "-")
    # Try with underscores too
    return mapping.get(key) or mapping.get(key.replace("-", "_")) or ""


def build_context_block_for_prompt(orders):
    """Turn list of Order objects into a text block suitable for system prompt."""
    if not orders:
        return ""
    lines = ["DETECTED CUSTOMER CONTEXT (real data from your store):"]
    for o in orders:
        s = serialize_order_for_ai(o)
        verified = s.get("verified_sender", False)
        verify_tag = "✓ VERIFIED" if verified else "⚠️ UNVERIFIED (sender email does NOT match this order's customer_email)"
        line = f"- Order #{s['order_number']} [{verify_tag}]"
        if s["customer_name"]:           line += f" · Customer: {s['customer_name']}"
        if s["product"]:                 line += f" · Product: {s['product']}"
        if s["total"]:                   line += f" · Total: {s['total']}"

        # Payment + fulfillment — show raw code AND human meaning
        if s["payment_status"]:
            meaning = _humanize_status(s["payment_status"], _PAYMENT_STATUS_MEANINGS)
            if meaning:
                line += f" · Payment: {s['payment_status']} ({meaning})"
            else:
                line += f" · Payment: {s['payment_status']}"
        if s["fulfillment_status"]:
            meaning = _humanize_status(s["fulfillment_status"], _FULFILLMENT_STATUS_MEANINGS)
            if meaning:
                line += f" · Fulfillment: {s['fulfillment_status']} ({meaning})"
            else:
                line += f" · Fulfillment: {s['fulfillment_status']}"

        if s["live_tracking_status"]:    line += f" · Live status: {s['live_tracking_status']}"
        elif s["tracking_status"]:       line += f" · Status: {s['tracking_status']}"
        if s["tracking_number"]:
            tracking_part = f"{s['tracking_number']}"
            if s.get("tracking_company"):
                tracking_part += f" ({s['tracking_company']})"
            line += f" · Tracking #: {tracking_part}"
        if s["tracking_url"]:
            line += f" · 🔗 TRACKING LINK (use this verbatim in reply if customer asks about tracking): {s['tracking_url']}"
        if s["delivered_at"]:            line += f" · Delivered: {s['delivered_at'][:10]}"
        if s["city"] or s["country"]:    line += f" · Location: {s['city']} {s['country']}".strip()

        # Days since order — for ETA reasoning
        if s.get("days_since_order") is not None:
            line += f" · Placed: {s['days_since_order']} day(s) ago"
        lines.append(line)

    # Glossary so the AI ALWAYS gets it right, even for codes not pre-translated above.
    lines.append(
        "\nSTATUS CODE GLOSSARY (critical — never misinterpret):"
        "\n  • 'processing' = payment RECEIVED, order is being prepared for shipment. "
        "Do NOT tell the customer their payment is still being verified."
        "\n  • 'completed' / 'fulfilled' / 'shipped' = order has shipped."
        "\n  • 'on-hold' or 'pending' = payment NOT yet received."
        "\n  • 'cancelled' / 'refunded' / 'failed' = self-explanatory."
        "\n  • If payment_status is 'processing' or 'paid', the customer has ALREADY PAID. "
        "Their order is in the fulfillment queue."
    )
    lines.append("Use this real data to answer accurately. Never invent order numbers, statuses, or tracking info.")
    return "\n".join(lines)


def call_claude(prompt, system="You are a professional ecommerce customer support assistant.", max_tokens=1024):
    """Single helper to call Claude and return text, or raise on error."""
    client = get_claude_client()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


def clean_text(value):
    if not value:
        return ""

    decoded = decode_header(value)
    text = ""

    for part, enc in decoded:
        if isinstance(part, bytes):
            text += part.decode(enc or "utf-8", errors="ignore")
        else:
            text += part

    return text


def extract_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition"))

            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                return payload.decode(errors="ignore") if payload else ""

    payload = msg.get_payload(decode=True)
    return payload.decode(errors="ignore") if payload else ""


def extract_body_html(msg):
    """Extract the HTML part of a MIME email, returning empty string if none."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition"))
            if content_type == "text/html" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                return payload.decode(errors="ignore") if payload else ""
    return ""


def extract_attachments(msg):
    attachments = []

    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        filename = part.get_filename()

        if not filename and "attachment" not in disposition:
            continue

        if filename:
            filename = clean_text(filename)

        data = part.get_payload(decode=True)
        if not data:
            continue

        attachments.append({
            "filename": filename or "attachment",
            "content_type": part.get_content_type(),
            "data": data,
        })

    return attachments


def save_attachments(email_obj, msg):
    for att in extract_attachments(msg):
        EmailAttachment.objects.create(
            email=email_obj,
            filename=att["filename"],
            content_type=att["content_type"],
            file=ContentFile(att["data"], name=att["filename"]),
            size=len(att["data"]),
        )


def find_order_from_email(store, subject, body):
    text = f"{subject or ''} {body or ''}"

    patterns = [
        r"#(\d{3,})",
        r"order\s*#?\s*(\d{3,})",
        r"order number\s*#?\s*(\d{3,})",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)

        for order_number in matches:
            order = Order.objects.filter(
                store=store,
                external_order_id=str(order_number)
            ).first()

            if order:
                return order

    return None


def classify_email(subject, body):
    text = f"{subject or ''} {body or ''}".lower()

    if "refund" in text or "return" in text or "money back" in text:
        return "refund"

    if "tracking" in text or "shipment" in text or "delivery" in text or "where is my order" in text:
        return "shipping"

    if "change address" in text or "wrong address" in text:
        return "order_edit"

    return "support"


def quick_reply(email_body):
    text = (email_body or "").lower()

    if "refund" in text:
        return """Hello,

We understand your concern. Our refund policy allows returns within 7 days. Please share your order number.

Best regards,
Support Team"""

    if "tracking" in text or "order" in text:
        return """Hello,

Your order is being processed. Tracking will be shared soon.

Best regards,
Support Team"""

    if "address" in text:
        return """Hello,

Please share updated address. We will update it.

Best regards,
Support Team"""

    return None


def hybrid_reply(email_obj):
    reply = quick_reply(email_obj.body)

    if not reply:
        reply = generate_ai_reply(email_obj)

    return reply


def _load_training_profile(store):
    """Load AiTrainingProfile for a store. Returns None if not configured."""
    if not store:
        return None
    try:
        from .models import AiTrainingProfile
        return AiTrainingProfile.objects.filter(store=store).first()
    except Exception:
        return None


def _load_snippets(store, limit=10):
    """Load top-N knowledge snippets for the store."""
    if not store:
        return []
    try:
        from .models import KnowledgeSnippet
        return list(KnowledgeSnippet.objects.filter(store=store).order_by('order_idx', '-updated_at')[:limit])
    except Exception:
        return []


def _load_recent_corrections(store, limit=5):
    """
    Pull the most recent admin corrections (Item 11) so the AI can learn
    what to change in future replies.
    """
    if not store:
        return []
    try:
        from .models import AiReplyFeedback
        qs = AiReplyFeedback.objects.filter(
            store=store, feedback_type__in=['edit', 'complaint']
        ).order_by('-created_at')[:limit]
        return list(qs)
    except Exception:
        return []


def _format_corrections_block(corrections):
    """Turn a list of AiReplyFeedback rows into a few-shot 'what not to do' block."""
    if not corrections:
        return ""
    lines = ["PAST CORRECTIONS — patterns the admin previously fixed, so don't repeat the same mistakes:"]
    for i, c in enumerate(corrections, 1):
        ai = (c.ai_draft or "").strip().replace("\n", " ")
        fin = (c.final_text or "").strip().replace("\n", " ")
        note = (c.correction_note or "").strip()
        if not ai and not fin and not note:
            continue
        snippet = f"\n[{i}] "
        if c.feedback_type == 'complaint':
            snippet += "Customer complained about a previous reply."
            if fin: snippet += f" Better wording: \"{fin[:240]}\""
        else:
            if ai:  snippet += f"AI wrote: \"{ai[:200]}\""
            if fin: snippet += f"\n    Admin sent: \"{fin[:200]}\""
        if note:    snippet += f"\n    Why: {note[:200]}"
        lines.append(snippet)
    return "\n".join(lines)


def _is_after_hours(support_hours):
    """
    Best-effort: parse support_hours like 'Mon–Fri, 9am–6pm EST' and decide
    whether right now is OUTSIDE those hours. Returns True/False/None (None=can't tell).
    """
    if not support_hours:
        return None
    try:
        from datetime import datetime
        s = support_hours.lower()
        if "24/7" in s or "always" in s:
            return False
        # Extract first time and second time (e.g. '9am-6pm' / '9 am - 6 pm')
        m = re.search(r"(\d{1,2})\s*(am|pm)?\s*[-–to]+\s*(\d{1,2})\s*(am|pm)", s)
        if not m:
            return None
        h1 = int(m.group(1))
        ap1 = m.group(2) or m.group(4)
        h2 = int(m.group(3))
        ap2 = m.group(4)
        def to24(h, ap):
            if ap == "pm" and h < 12: return h + 12
            if ap == "am" and h == 12: return 0
            return h
        start = to24(h1, ap1 or ap2)
        end   = to24(h2, ap2)
        now_h = datetime.utcnow().hour  # approximate; per-timezone parsing is heavy
        # If the store mentions EST/PST/CST adjust
        if "est" in s or "edt" in s: now_h = (now_h - 4) % 24
        elif "pst" in s or "pdt" in s: now_h = (now_h - 7) % 24
        elif "cst" in s: now_h = (now_h - 6) % 24
        elif "mst" in s: now_h = (now_h - 7) % 24
        elif "gmt" in s or "utc" in s: pass
        in_hours = start <= now_h < end
        # Weekday check
        wd = datetime.utcnow().weekday()
        if ("mon-fri" in s or "mon–fri" in s or "weekday" in s) and wd > 4:
            return True
        return not in_hours
    except Exception:
        return None


def _detect_customer_language(body):
    """Lightweight heuristic to guess language from email body.
    Returns a hint string (None = can't tell, stay with default)."""
    if not body or len(body.strip()) < 8:
        return None
    body_l = body.lower()
    # Simple word-presence check — good enough to nudge the AI
    hints = [
        ("Spanish",  ["hola", "gracias", "por favor", "mi pedido", "envío", "dónde", "ayuda"]),
        ("French",   ["bonjour", "merci", "s'il vous plaît", "ma commande", "livraison", "où"]),
        ("German",   ["hallo", "danke", "bitte", "meine bestellung", "lieferung", "hilfe"]),
        ("Italian",  ["ciao", "grazie", "per favore", "il mio ordine", "spedizione"]),
        ("Portuguese", ["olá", "obrigado", "obrigada", "meu pedido", "entrega"]),
        ("Arabic",   ["مرحبا", "شكرا", "طلبي", "أين"]),
        ("Urdu",     ["mera order", "kahan hai", "shukria", "kab aayega"]),
        ("Roman Urdu+English", ["bhai", "aap ka", "kahan", "kab"]),
    ]
    for lang_name, kws in hints:
        if any(k in body_l for k in kws):
            return lang_name
    return None


def build_training_system_prompt(profile, snippets, detected_context="",
                                  customer_lang_hint=None, after_hours=None,
                                  admin_note_hint=None):
    """
    Compose the full system prompt for Claude using the per-store
    AiTrainingProfile + KnowledgeSnippets + (optional) detected customer context.
    Returns a string.
    """
    biz   = (getattr(profile, 'business_name', '') or '').strip() if profile else ''
    niche = (getattr(profile, 'niche', '')         or '').strip() if profile else ''
    desc  = (getattr(profile, 'description', '')   or '').strip() if profile else ''
    lang  = (getattr(profile, 'language', '')      or 'English').strip() if profile else 'English'
    hours = (getattr(profile, 'support_hours', '') or '').strip() if profile else ''
    signoff = (getattr(profile, 'signoff', '')     or '').strip() if profile else ''
    voice_eg = (getattr(profile, 'voice_example', '') or '').strip() if profile else ''
    tones = getattr(profile, 'tones', []) if profile else []
    rlen  = (getattr(profile, 'reply_length', '')  or 'Medium').strip() if profile else 'Medium'

    tones_str = ', '.join(tones) if tones else 'professional, warm'
    length_map = {
        'Short (1–2 sentences)': '1–2 short sentences (under 40 words total)',
        'Medium (3–5 sentences)': '3–4 short sentences (under 80 words total)',
        'Detailed (paragraph)':   'two short paragraphs maximum',
    }
    length_str = length_map.get(rlen, '2–3 short sentences (under 60 words total)')

    biz_label = biz or 'our store'
    niche_label = niche or 'an ecommerce brand'
    system = f'You are a customer support agent for "{biz_label}" — {niche_label}.'
    if desc:    system += f"\n{desc}"
    if hours:   system += f"\nSupport hours: {hours}."
    system += f"\n\nTone: {tones_str}. Reply length: {length_str}. Language: {lang}."
    if signoff: system += f'\nSign-off every reply with: "{signoff}".'

    if voice_eg:
        system += f"\n\nBRAND VOICE EXAMPLE (mimic this style):\n\"{voice_eg}\""

    if snippets:
        system += "\n\nRELEVANT KNOWLEDGE BASE:"
        for i, s in enumerate(snippets[:8], start=1):
            system += f"\n[{i}] ({s.category}) {s.title}: {s.text}"

    if detected_context:
        system += f"\n\n{detected_context}"

    # Language override placeholder will be replaced; we want corrections in the system prompt too
    # — but we don't have the corrections var here. The caller injects it via the user message
    # (see generate_ai_reply). Keep this function focused on profile-derived content. — if the customer wrote in a different language than the profile default
    if customer_lang_hint and customer_lang_hint.lower() not in lang.lower():
        system += (
            f"\n\n🌐 LANGUAGE OVERRIDE: The customer appears to be writing in {customer_lang_hint}. "
            f"Reply in {customer_lang_hint} (NOT the profile default of {lang})."
        )

    # After-hours acknowledgment
    if after_hours is True and hours:
        system += (
            f"\n\n🌙 AFTER-HOURS: It's currently outside support hours ({hours}). "
            "Open the reply with a brief, transparent note like 'Our team is offline right now, but I can help immediately:' "
            "then answer normally. Do NOT promise human follow-up on a specific timeline unless explicitly known."
        )

    # Admin-note context — AI is being told this is a flagged email
    if admin_note_hint:
        system += (
            f"\n\n⚠️ INTERNAL FLAG (for your awareness): {admin_note_hint} "
            "Be EXTRA careful — keep promises minimal, avoid commitments on policy you're unsure about, "
            "and lean on phrases like 'I'm flagging this for our team for fast review.'"
        )

    # Strong formatting + brevity rules — these are the most important part of the prompt.
    system += (
        "\n\nREQUIRED REPLY FORMAT (output your reply EXACTLY in this structure with real blank lines):"
        "\n"
        "\nHi <First Name>,"
        "\n"
        "\n<One short paragraph answering the question directly.>"
        "\n"
        "\n<Sign-off line>"
        "\n"
        "\nEvery section is separated by a TRUE empty line (an actual blank line, i.e. two newlines)."
        "\nDo NOT put the sign-off on the same line as the body. Do NOT collapse this into a single paragraph."
        "\n"
        "\nSTRICT REPLY RULES (must follow):"
        "\n1. Be SHORT and to the point. No filler, no over-explaining."
        "\n2. Open with a brief greeting line (e.g., 'Hi <Name>,'). If a CUSTOMER'S FIRST NAME is provided in the user message, USE IT VERBATIM. Otherwise use a generic greeting like 'Hi there,'. SKIP the greeting if you are already mid-conversation and just answering a quick follow-up."
        "\n3. Body: ONE focused short paragraph that answers the question directly."
        "\n4. Use a BLANK LINE between the greeting, the body, and the sign-off — this is non-negotiable for readability. The sign-off must NEVER appear inline with the body."
        "\n5. Skip generic phrases like 'Thanks for reaching out!', 'I hope this helps', 'Let me know if you have any other questions' unless they genuinely add value."
        "\n6. Never invent order numbers, tracking, statuses, or dates — use ONLY the DETECTED CUSTOMER CONTEXT block (if present). If data is missing, ask for it."
        "\n7. Sound human and confident, not robotic. Match the brand voice example above."
        "\n8. Output the reply as plain text only — no HTML, no markdown, no subject line."

        "\n\nCONVERSATION CONTEXT RULES (very important — read every time):"
        "\nA. If a PRIOR CONVERSATION block is shown, you are mid-thread. Read it before replying."
        "\nB. NEVER repeat information you already shared earlier in this thread (e.g. order number, status, ETA, tracking, brand name). The customer already has it."
        "\nC. If the customer is just CONFIRMING or ACKNOWLEDGING something you said (e.g. 'Ok so it's processing right?', 'Got it', 'Thanks for confirming'), reply in ONE SHORT SENTENCE that simply confirms (e.g. 'Yes, that's right — it's still processing.'). Do not restate the full order details."
        "\nD. If the customer is asking a NEW question, answer just THAT — don't re-summarize prior context."
        "\nE. Do not re-greet (e.g. don't say 'Hi <Name>' again) for messages that are clearly part of an ongoing back-and-forth."
        "\nF. The sign-off rule still applies: drop the sign-off only if you are doing a one-sentence quick confirmation."

        "\n\nETA + TRACKING RULES (use ACTUAL data, not vague promises):"
        "\nG. If 'Placed: N day(s) ago' is in the context, use that to compute a real ETA. "
        "Example: 'Your order was placed 3 days ago — typical fulfillment is 1–2 business days, so it ships any day now.'"
        "\nH. If a 🔗 TRACKING LINK URL is present in the context, INCLUDE IT VERBATIM in the reply when the customer asks about tracking. "
        "Format it on its own line so it's clickable. NEVER paraphrase the URL."
        "\nI. If fulfillment_status is 'completed' or 'shipped' and a tracking URL exists, give the customer the link directly — don't promise to 'send tracking later'."
        "\nJ. If tracking number exists but tracking URL doesn't, include just the tracking number and the courier name (e.g., 'Tracking: 1Z999AA10123456784 — FedEx')."

        "\n\n🔒 ORDER VERIFICATION RULES (CRITICAL — protects customer privacy):"
        "\nP. Each order in DETECTED CUSTOMER CONTEXT is tagged either [✓ VERIFIED] or [⚠️ UNVERIFIED]."
        "\nQ. For [✓ VERIFIED] orders — the sender's email matches the order's customer_email, so it's THEIR order. "
        "You can share full details (status, tracking, product, ETA, etc.)."
        "\nR. For [⚠️ UNVERIFIED] orders — the sender is asking about an order that is NOT under their email. "
        "Do NOT share status, tracking, products, customer name, total, or any specific details. "
        "Politely ask the customer to verify ownership before continuing. Examples:"
        "\n     • 'To protect your order's privacy, I need to verify it belongs to you. Could you confirm the email address used at checkout, or share the billing postcode?'"
        "\n     • 'I see that order number, but it's not associated with the email you're writing from. Could you send this from the original order email, or share the billing name + postcode so I can verify?'"
        "\nS. If the customer is asking about an order that was NOT FOUND in our store at all (no row in DETECTED CUSTOMER CONTEXT), "
        "say: 'I couldn't find that order in our system. Could you double-check the order number and the email used at checkout?'"
        "\nT. NEVER reveal customer_name, address, email, phone, or any PII from an UNVERIFIED order — even by accident in a confirmation."

        "\n\nFACT-CHECK RULES (CRITICAL — never agree with a false premise):"
        "\nK. ALWAYS fact-check the customer's claim against the DETECTED CUSTOMER CONTEXT before responding."
        "\nL. If the customer mentions tracking ('my tracking link is not working', 'tracking shows X'):"
        "\n     • Check the context — does this order have a tracking_number or tracking_url?"
        "\n     • If NO tracking exists yet (status is 'processing' / 'pending') → DO NOT apologize for a broken link. "
        "Tell the truth politely: 'A tracking link hasn't been generated for your order yet — it's still being prepared. "
        "You'll receive the link by email as soon as it ships.'"
        "\n     • If tracking DOES exist but they say it's broken → re-share the exact URL and offer to look into it."
        "\nM. If the customer says they received an email/refund/notification but the order data shows the opposite, "
        "gently clarify the actual status. Example: 'I checked your order and don't see a tracking email having been sent yet — "
        "your order is still being prepared.'"
        "\nN. NEVER apologize for 'confusion in my earlier message' or 'my mistake' unless there is a real prior mistake "
        "visible in the PRIOR CONVERSATION block. Phantom apologies erode trust."
        "\nO. NEVER confirm something that isn't in the order data. If the customer claims something you can't verify, "
        "ask them to clarify ('Could you share the tracking number you're trying to use?') instead of inventing a confirmation."
    )
    return system


def _extract_customer_first_name(email_obj, order=None):
    """
    Best-effort: pull the customer's first name for greeting.
    Priority: Order.customer_name → display name from sender header → ''
    """
    # 1. From the linked order's customer_name (most reliable)
    if order is not None and getattr(order, "customer_name", ""):
        name = (order.customer_name or "").strip()
        first = name.split()[0] if name else ""
        if first and len(first) > 1:
            return first
    # 2. From the sender display name (e.g., "Syncere Davis <foo@bar.com>")
    raw = (getattr(email_obj, "sender", "") or "").strip()
    m = re.match(r'^\s*"?([^"<]+?)"?\s*<', raw)
    if m:
        display = m.group(1).strip()
        # Skip obvious aliases/team names
        bad = {"sales", "team", "support", "no-reply", "noreply", "info", "admin", "service"}
        first = display.split()[0] if display else ""
        if first and first.lower() not in bad and len(first) > 1 and first.isalpha():
            return first
    return ""


def _enforce_reply_structure(text):
    """
    Post-process AI reply to guarantee proper paragraph spacing.
    The AI sometimes outputs one-line replies; we force:
      Greeting,
      <blank>
      Body
      <blank>
      Sign-off
    Idempotent — if structure is already correct, leaves it alone.
    """
    if not text:
        return text
    s = text.strip()

    # 1. Ensure a blank line after a greeting line like "Hi Name," or "Hello Name,"
    s = re.sub(
        r"^(Hi|Hello|Hey|Dear|Greetings)\s+[^,\n]{1,40},\s*(?!\n)",
        lambda m: m.group(0).rstrip() + "\n\n",
        s,
        flags=re.IGNORECASE | re.MULTILINE,
    )

    # 2. Ensure a blank line BEFORE a sign-off — but only on the FIRST signoff occurrence
    # and only when it's preceded by a sentence-ending punctuation + space (not already on its own line).
    signoff_starters = [
        "Best regards", "Best wishes", "Best,",
        "Warm regards", "Kind regards", "Sincerely", "Cheers,",
        "Thanks!", "Thank you", "Take care", "Many thanks",
        "Yours truly", "Yours sincerely", "Regards,",
    ]
    pattern = r"([.!?])\s+(" + "|".join(re.escape(p) for p in signoff_starters) + r")"
    s = re.sub(pattern, lambda m: m.group(1) + "\n\n" + m.group(2), s, count=1)

    # 3. Collapse any run of >2 newlines down to exactly 2
    s = re.sub(r"\n{3,}", "\n\n", s)

    return s.strip()


def _build_thread_history_text(email_obj, account, max_messages=8, max_chars_per_msg=600):
    """
    Return a string of prior messages in the same conversation thread so the AI
    can see what was already discussed and avoid repeating info.
    """
    if not account:
        return ""
    try:
        from .views import get_thread_contact, extract_clean_email
    except Exception:
        return ""

    contact = (get_thread_contact(email_obj) or "").lower()
    if not contact:
        return ""

    store_email = (getattr(account, "email", "") or "").lower()

    # All prior messages for this store, excluding the current email, oldest first
    prior_qs = (EmailMessage.objects
                .filter(store=email_obj.store)
                .exclude(id=email_obj.id)
                .order_by("created_at"))

    history = []
    for m in prior_qs:
        # Only keep messages that belong to this conversation
        try:
            m_contact = (get_thread_contact(m) or "").lower()
        except Exception:
            m_contact = ""
        if m_contact != contact:
            continue

        m_sender = extract_clean_email(m.sender or "")
        role = "Support (you)" if m_sender == store_email else "Customer"
        body = (m.body or "").strip()
        if not body:
            continue
        if len(body) > max_chars_per_msg:
            body = body[:max_chars_per_msg].rstrip() + " …(truncated)"
        history.append(f"{role}:\n{body}")

    if not history:
        return ""

    # Keep only the last N turns to control prompt size
    history = history[-max_messages:]
    return "\n\n---\n\n".join(history)


def generate_ai_reply(email_obj, account=None):
    """
    Generate an AI reply for a customer email using:
      1. AiTrainingProfile (per store) — brand, tone, language, voice
      2. KnowledgeSnippets  (per store) — FAQs, policies
      3. Auto-detected customer refs   — order #s, emails, tracking
      4. Resolved Order data           — real status, tracking, customer info
      5. Prior conversation history in the same thread
    Falls back to per-EmailAccount settings if no profile is set.
    """
    # Lookup tenant store via email_obj
    store = getattr(email_obj, 'store', None)

    # Try to load the per-store training profile + snippets + past corrections (Item 11)
    profile = _load_training_profile(store)
    snippets = _load_snippets(store)
    corrections = _load_recent_corrections(store)
    corrections_block = _format_corrections_block(corrections)

    # Auto-extract customer refs from email subject + body + sender
    scan_text = " ".join([
        getattr(email_obj, 'subject', '') or '',
        getattr(email_obj, 'body', '')    or '',
        getattr(email_obj, 'sender', '')  or '',
    ])
    detected_refs = extract_customer_refs(scan_text)
    resolved = resolve_customer_context(
        detected_refs,
        sender_email=getattr(email_obj, 'sender', ''),
        store=store,
    )
    detected_orders = resolved.get('orders') or []
    detected_block  = build_context_block_for_prompt(detected_orders)

    # Conversation history (so AI doesn't repeat info already shared in this thread)
    history_text = _build_thread_history_text(email_obj, account)

    # Customer first-name hint (avoids the AI using the wrong account name)
    linked_order = (detected_orders[0] if detected_orders else None)
    customer_first_name = _extract_customer_first_name(email_obj, order=linked_order)

    # Detect customer language from the new message (Item 6)
    customer_lang_hint = _detect_customer_language(getattr(email_obj, 'body', '') or '')

    # After-hours awareness (Item 7)
    after_hours = _is_after_hours(getattr(profile, 'support_hours', '') if profile else '')

    # Admin note hint (Item 9) — gate may have flagged this email
    raw = getattr(email_obj, 'raw_data', None) or {}
    admin_note_hint = raw.get('admin_note') or None

    # ── Path A: New training-profile flow ──────────────────────────────────
    if profile or snippets:
        system_prompt = build_training_system_prompt(
            profile, snippets, detected_block,
            customer_lang_hint=customer_lang_hint,
            after_hours=after_hours,
            admin_note_hint=admin_note_hint,
        )

        # Compose the user-side message (subject + body), prefixed with prior thread history
        subj = (getattr(email_obj, 'subject', '') or '').strip()
        body = (getattr(email_obj, 'body', '') or '').strip()
        new_msg = (f"Subject: {subj}\n\n{body}" if subj else body) or "(empty email)"

        parts = []
        if corrections_block:
            parts.append(corrections_block)
        if customer_first_name:
            parts.append(f"CUSTOMER'S FIRST NAME (use this in the greeting): {customer_first_name}")
        if history_text:
            parts.append("PRIOR CONVERSATION IN THIS THREAD (oldest → newest):\n\n" + history_text)
        parts.append("CUSTOMER'S NEW MESSAGE (reply to THIS only):\n\n" + new_msg)
        user_msg = "\n\n===\n\n".join(parts)

        try:
            reply = call_claude(prompt=user_msg, system=system_prompt, max_tokens=1024)
        except Exception as e:
            print("AI REPLY ERROR (training-profile flow):", str(e))
            reply = generate_auto_draft(email_obj)

        # Force proper paragraph structure (greeting / body / sign-off blank lines)
        reply = _enforce_reply_structure(reply)

        # Append per-inbox signature if requested
        if account:
            use_signature = getattr(account, 'ai_use_signature', False)
            signature = (getattr(account, 'signature', '') or '').strip()
            if use_signature and signature:
                reply = reply.rstrip() + "\n\n--\n" + signature
        return reply

    # ── Path B: Legacy per-EmailAccount flow (backward compatible) ─────────
    tone_map = {
        "formal":     "Formal",
        "friendly":   "Friendly & Professional",
        "concise":    "Concise",
        "empathetic": "Empathetic",
    }
    lang_map = {
        "english": "English", "arabic": "Arabic",
        "urdu":    "Urdu",    "french": "French",
    }

    tone = "Friendly & Professional"
    language = "English"
    include_order = True
    custom_instructions = ""
    use_signature = True
    signature = ""

    if account:
        tone = tone_map.get(account.ai_tone, "Friendly & Professional")
        language = lang_map.get(account.ai_language, "English")
        include_order = account.ai_include_order
        custom_instructions = (account.ai_custom_instructions or "").strip()
        use_signature = account.ai_use_signature
        signature = (account.signature or "").strip()

    # Use auto-detected context if available, else fall back to the
    # legacy email_obj.order attribute (set elsewhere in the codebase).
    order_context = ""
    if detected_block:
        order_context = "\n\n" + detected_block
    elif include_order and getattr(email_obj, "order", None):
        o = email_obj.order
        order_context = f"\n\nOrder context: #{getattr(o, 'external_order_id', o.id)}, Status: {getattr(o, 'status', 'unknown')}"

    custom_block = ""
    if custom_instructions:
        custom_block = f"\n\nTenant-specific instructions (MUST follow):\n{custom_instructions}"

    history_block = ""
    if history_text:
        history_block = (
            "\n\nPRIOR CONVERSATION IN THIS THREAD (oldest → newest):\n"
            f"{history_text}\n"
        )

    prompt = f"""{history_block}Customer's new message:
{email_obj.body}{order_context}{custom_block}

Write a {tone} ecommerce support reply in {language}.

STRICT REPLY RULES (must follow):
1. SHORT and to the point — 2 to 3 short sentences max (under 60 words total).
2. Start with a brief greeting on its own line (e.g., "Hi <Name>,"). SKIP the greeting if you are already mid-conversation and just answering a quick follow-up.
3. Leave a BLANK LINE between greeting, body, and sign-off — this is required for readability.
4. Skip filler phrases like "Thanks for reaching out!", "I hope this helps", "Let me know if you need anything else" unless they genuinely add value.
5. NEVER invent order numbers, statuses, or tracking — use ONLY the Order context block if present. If data is missing, ask for it.
6. Sound human and professional, not robotic.

CONVERSATION CONTEXT RULES:
- If a PRIOR CONVERSATION block is shown above, you are mid-thread. NEVER repeat info you already shared (order #, status, ETA, tracking).
- If the customer is just CONFIRMING or ACKNOWLEDGING (e.g. "ok so it's processing right?", "got it"), reply in ONE SHORT SENTENCE that simply confirms — no full restate, no greeting, no sign-off.
- If the customer asks a NEW question, answer just THAT.

FACT-CHECK RULES (CRITICAL — never agree with a false premise):
- ALWAYS fact-check the customer's claim against the Order context block before responding.
- If the customer mentions tracking ("my tracking link is not working") but no tracking has been generated yet (status is "processing"/"pending"), DO NOT apologize for a broken link. Tell the truth: "A tracking link hasn't been generated yet — your order is still being prepared. You'll receive it by email as soon as it ships."
- NEVER apologize for "confusion in my earlier message" or "my mistake" unless there is a real prior mistake visible in the PRIOR CONVERSATION block.
- NEVER confirm something that isn't in the order data. If the customer claims something you can't verify, ASK them to clarify.

Plain text only — no HTML, no markdown, no subject line. The sign-off will be appended separately (unless you skipped greeting because it's a quick confirmation)."""

    try:
        reply = call_claude(prompt)
    except Exception as e:
        print("AI REPLY ERROR:", str(e))
        reply = generate_auto_draft(email_obj)

    if use_signature and signature:
        reply = reply.rstrip() + "\n\n--\n" + signature

    return reply


def generate_ai_text(prompt):
    try:
        return call_claude(prompt)
    except Exception as e:
        print("AI TEXT ERROR:", str(e))
        return None


# ────────────────────────────────────────────────────────────────────────
# AI Auto-Reply Mode Handler
# ────────────────────────────────────────────────────────────────────────

SIMPLE_AUTO_KEYWORDS = {
    "tracking", "track", "where is my order", "where is my package",
    "shipped", "delivery", "delivered",
    "order status", "status of my order",
    "thank you", "thanks", "received",
}

def _is_simple_email(body):
    """Hybrid mode: decide if email is simple enough for auto-send."""
    if not body:
        return False
    text = body.lower()
    if len(text) > 600:
        return False
    return any(k in text for k in SIMPLE_AUTO_KEYWORDS)


def _extract_reply_to(email_obj):
    """Pull the customer's address from email_obj.sender (e.g. 'Name <a@b.com>')."""
    raw = (email_obj.sender or "").strip()
    m = re.search(r"<([^>]+)>", raw)
    return (m.group(1) if m else raw).strip()


def score_reply_confidence(customer_message, ai_reply, context_block=""):
    """
    Self-rate AI reply quality. Returns a float 0.0–1.0.
    Uses a small Claude call — keep it short to control cost.

    context_block: the DETECTED CUSTOMER CONTEXT block (real order data the AI
    used to write the reply). Passing this lets the rater verify whether
    order numbers / tracking / status mentioned in the reply are REAL vs invented.
    """
    ctx_section = ""
    if context_block:
        ctx_section = "REAL ORDER/CUSTOMER DATA AVAILABLE TO THE AI:\n" + context_block + "\n\n"
    rater_prompt = f"""Audit this customer-support AI reply.

{ctx_section}CUSTOMER'S MESSAGE:
{(customer_message or '')[:1500]}

AI'S DRAFTED REPLY:
{(ai_reply or '')[:1500]}

Rate from 0–100 how safe it is to AUTO-SEND this reply (no human review).

Scoring guide:
  90–100 = Reply is accurate, on-topic, no fabrication, safe to send.
  70–89  = Mostly fine, minor issues but still safe.
  40–69  = Risky: vague, slightly off-topic, or makes soft promises.
  0–39   = NOT safe: fabricates order data, hallucinates tracking, off-topic, or could mislead.

If the reply uses order numbers / tracking / dates that are present in the REAL ORDER DATA above, that's GOOD and should score HIGH. Only mark down for INVENTED data that isn't in the context.

Output EXACTLY one line:
SCORE: <0-100>"""
    try:
        text = call_claude(
            rater_prompt,
            system="You are a strict but FAIR quality auditor. Output only 'SCORE: N'.",
            max_tokens=30,
        )
        m = re.search(r"SCORE\s*:\s*(\d+)", text or "", re.IGNORECASE)
        if m:
            return max(0.0, min(1.0, int(m.group(1)) / 100.0))
    except Exception as e:
        print("CONFIDENCE SCORE ERROR:", e)
    return 1.0  # On rater failure, don't block auto-send


# ════════════════════════════════════════════════════════════════════════════
# 🛡️ AUTO-REPLY SAFETY GATES (run BEFORE generate_ai_reply)
# ════════════════════════════════════════════════════════════════════════════

# Sender patterns that should NEVER get an auto-reply (newsletters, bots, no-reply)
_BOT_SENDER_PATTERNS = [
    r"\bno[-_.]?reply\b",
    r"\bdo[-_.]?not[-_.]?reply\b",
    r"\bnoreply\b",
    r"\bdonotreply\b",
    r"\bmailer[-_.]?daemon\b",
    r"\bpostmaster\b",
    r"\bautomated?[-_.]?(?:reply|response|message)\b",
    r"\bnotifications?[-_.]?(?:noreply|donotreply)\b",
    r"\bbounce(?:s)?[-_.]?",
]
_BOT_DOMAIN_PATTERNS = [
    # Subdomain on domain (`@news.brand.com`, `@email.brand.com`, ...)
    r"@news[-_.]", r"@email[-_.]", r"@mail[-_.]", r"@info[-_.]",
    r"@notifications?[-_.]", r"@updates?[-_.]", r"@marketing[-_.]",
    r"@offers?[-_.]", r"@promo[-_.]", r"@newsletter[-_.]?",
    r"@em[-_.]", r"@e[-_.]?mail", r"@bounce",
    # Any domain containing 'newsletter'
    r"@[a-z0-9-]*newsletter[a-z0-9-]*\.",
    # Common marketing platforms
    r"@.*\.(?:mailchimp|sendgrid|customeriomail|sparkpostmail|mandrillapp)\.",
]

# Header patterns that indicate a bulk / automated mail
_BULK_HEADER_TOKENS = ("unsubscribe", "list-unsubscribe", "auto-submitted")

# Anger / hostile customer keywords — these emails go to human, never auto-send
_ANGER_KEYWORDS = {
    "lawsuit", "lawyer", "attorney", "legal action", "sue", "suing",
    "scam", "scammer", "fraud", "fraudulent", "cheat", "cheated",
    "rip off", "ripped off", "ripoff",
    "outrageous", "ridiculous", "disgusting", "pathetic", "useless",
    "terrible service", "horrible service", "worst service",
    "report you", "reporting you", "bbb", "trustpilot",
    "chargeback", "charge back", "dispute the charge",
    "this is unacceptable", "i'm furious", "i am furious", "fed up",
    "small claims", "consumer protection",
}

# High-stakes request keywords — refund / address change → human approval
_HIGHSTAKES_KEYWORDS = {
    "refund me", "refund my money", "want a refund", "demand a refund",
    "change my address", "update my address", "wrong address",
    "cancel my order", "cancel the order", "cancellation",
    "didn't receive", "did not receive", "never received",
    "missing item", "wrong item", "damaged item", "broken",
    "return this", "i want to return", "send it back",
}

# Reply-loop detection — our own outbound replies / auto-acknowledgements
_LOOP_PATTERNS = [
    r"auto[-_]?submitted:\s*auto[-_](generated|replied)",
    r"this is an automated message",
    r"do not reply to this email",
    r"\bvacation auto[-_]?reply\b",
]


def _matches_any(patterns, text):
    text = (text or "").lower()
    return any(re.search(p, text) for p in patterns)


def _gate_check(email_obj):
    """
    Run all safety/escalation gates BEFORE calling Claude.
    Returns a dict:
        {action: 'auto'|'draft'|'skip', reason: str, admin_note: str|None}
      auto  → safe to auto-send
      draft → generate AI draft but DO NOT send (admin reviews)
      skip  → don't even bother generating (newsletter/bot/loop)
    """
    sender = (email_obj.sender or "").lower()
    subject = (email_obj.subject or "")
    body = (email_obj.body or "")
    body_l = body.lower()
    raw_headers = ""

    # raw_data can have message headers we recorded on sync
    raw = getattr(email_obj, "raw_data", None) or {}
    # Sender-based bot detection
    if _matches_any(_BOT_SENDER_PATTERNS, sender):
        return {"action": "skip",
                "reason": f"Bot/no-reply sender ({sender[:60]})",
                "admin_note": None}
    if _matches_any(_BOT_DOMAIN_PATTERNS, sender):
        return {"action": "skip",
                "reason": f"Bulk/marketing domain ({sender[:60]})",
                "admin_note": None}

    # Auto-submitted / vacation reply / our own auto-reply loop
    if _matches_any(_LOOP_PATTERNS, body_l) or _matches_any(_LOOP_PATTERNS, subject.lower()):
        return {"action": "skip",
                "reason": "Auto-submitted / loop guard",
                "admin_note": None}

    # Unsubscribe / list-unsubscribe headers in body → newsletter
    if any(tok in body_l for tok in _BULK_HEADER_TOKENS):
        # Only skip if it really looks like a newsletter (long + unsubscribe link)
        if len(body) > 800 and "unsubscribe" in body_l:
            return {"action": "skip",
                    "reason": "Newsletter (unsubscribe link present)",
                    "admin_note": None}

    # Anger / legal threats → escalate to human
    matched_anger = [k for k in _ANGER_KEYWORDS if k in body_l]
    if matched_anger:
        return {"action": "draft",
                "reason": "Angry / hostile language detected",
                "admin_note": f"⚠️ ESCALATE: customer used hostile language ({', '.join(matched_anger[:3])}). Review carefully before sending."}

    # High-stakes (refund/cancel/return) → human approval
    matched_hs = [k for k in _HIGHSTAKES_KEYWORDS if k in body_l]
    if matched_hs:
        return {"action": "draft",
                "reason": "High-stakes request (refund/cancel/return)",
                "admin_note": f"⚠️ HIGH-STAKES: customer is asking for {matched_hs[0]}. AI drafted a cautious reply — please review."}

    # Empty/very short body → still safe to auto, but flag it
    return {"action": "auto", "reason": "Safe to auto-send", "admin_note": None}


def process_ai_reply_mode(email_obj, account):
    """
    Generate AI draft and act based on account.ai_reply_mode:
        off     → save no AI draft, fall back to quick_reply / auto_draft
        suggest → generate draft, save to ai_draft (do NOT send)
        auto    → generate draft AND send via tenant Gmail
        hybrid  → if simple → auto-send, else suggest
    Returns dict with 'mode_used' and 'sent' boolean.
    """
    mode = getattr(account, "ai_reply_mode", "off") or "off"
    result = {"mode_used": mode, "sent": False, "draft": "", "gate": None}

    # Off → quick keyword draft only
    if mode == "off":
        email_obj.ai_draft = quick_reply(email_obj.body) or generate_auto_draft(email_obj)
        result["draft"] = email_obj.ai_draft
        return result

    # ── SAFETY GATE: skip newsletters/bots, escalate angry/high-stakes ─────
    gate = _gate_check(email_obj)
    result["gate"] = gate

    if gate["action"] == "skip":
        # Don't even generate — silently drop. Mark on raw_data so we can audit.
        email_obj.status = "drafted"  # leaves it visible but unanswered
        raw = getattr(email_obj, "raw_data", None) or {}
        raw["ai_auto_reply_skipped"] = gate["reason"]
        email_obj.raw_data = raw
        result["draft"] = ""
        return result

    # Generate the AI draft (used by all other modes)
    try:
        draft = generate_ai_reply(email_obj, account)
    except Exception as e:
        print("AI REPLY MODE — generation failed:", e)
        draft = quick_reply(email_obj.body) or generate_auto_draft(email_obj)

    email_obj.ai_draft = draft
    result["draft"] = draft

    # Persist admin note if gate flagged this email for review
    if gate.get("admin_note"):
        raw = getattr(email_obj, "raw_data", None) or {}
        raw["admin_note"] = gate["admin_note"]
        raw["escalated_reason"] = gate["reason"]
        email_obj.raw_data = raw

    # Decide whether to auto-send
    should_send = False
    if mode == "auto" and gate["action"] == "auto":
        should_send = True
    elif mode == "hybrid" and gate["action"] == "auto" and _is_simple_email(email_obj.body):
        should_send = True
    # If gate said 'draft', we never auto-send regardless of mode

    # ── CONFIDENCE GATE (Item 10) ──
    # If we're about to auto-send, ask Claude to self-rate the reply first.
    # If confidence < the profile threshold (default 0.7) → demote to draft.
    confidence_threshold = 0.7
    try:
        store = getattr(email_obj, 'store', None)
        profile = _load_training_profile(store)
        # AI Training Studio toggle: "Escalate if confidence < 70%"
        toggles = (getattr(profile, 'toggles', {}) or {}) if profile else {}
        # Honor the toggle if explicitly off
        confidence_enabled = toggles.get('Escalate to human if AI confidence < 70%', True)
    except Exception:
        confidence_enabled = True

    if should_send and confidence_enabled:
        try:
            # Pass the detected order context so the rater can verify
            # whether tracking/order numbers in the reply are real vs invented
            _store = getattr(email_obj, 'store', None)
            _scan = " ".join([email_obj.subject or "", email_obj.body or "", email_obj.sender or ""])
            _refs = extract_customer_refs(_scan)
            _resolved = resolve_customer_context(_refs, sender_email=email_obj.sender, store=_store)
            _ctx = build_context_block_for_prompt(_resolved.get('orders') or [])

            confidence = score_reply_confidence(
                customer_message=email_obj.body or "",
                ai_reply=draft or "",
                context_block=_ctx,
            )
            raw_now = getattr(email_obj, "raw_data", None) or {}
            raw_now["ai_reply_confidence"] = confidence
            email_obj.raw_data = raw_now
            if confidence < confidence_threshold:
                # Demote: don't auto-send, save as draft for human review
                should_send = False
                raw_now["admin_note"] = (
                    (raw_now.get("admin_note") or "")
                    + f" ⚠️ AI confidence {int(confidence*100)}% < {int(confidence_threshold*100)}% — demoted to draft."
                ).strip()
        except Exception as e:
            print("AI CONFIDENCE check failed (allow send):", e)

    if should_send:
        try:
            recipient = _extract_reply_to(email_obj)
            if not recipient:
                raise ValueError("No recipient address found")

            subject = email_obj.subject or "Re: Your message"
            if not subject.lower().startswith("re:"):
                subject = "Re: " + subject

            # Pull threading info from raw_data so the reply lands in the SAME
            # Gmail conversation thread instead of starting a new one each time.
            raw = getattr(email_obj, "raw_data", None) or {}
            in_reply_to = (raw.get("message_id") or "").strip() or None
            prior_refs  = (raw.get("references") or "").strip()
            references  = (f"{prior_refs} {in_reply_to}".strip()
                           if (prior_refs and in_reply_to) else (in_reply_to or prior_refs or None))
            thread_id   = (raw.get("thread_id") or "").strip() or None

            # send via tenant's connected Gmail / SMTP
            from .views import send_email_with_store_account
            html_body = draft.replace("\n", "<br>")
            send_email_with_store_account(
                account.store, recipient, subject, html_body,
                in_reply_to=in_reply_to,
                references=references,
                thread_id=thread_id,
            )
            email_obj.status = "replied"
            result["sent"] = True

            # Persist the outgoing AI reply as a new EmailMessage so it shows in the
            # dashboard's chat thread (was previously sent but invisible in UI).
            try:
                from_addr = getattr(account, "email", "") or ""
                EmailMessage.objects.create(
                    store=account.store,
                    sender=from_addr,
                    recipient=recipient,
                    subject=subject,
                    body=draft,
                    status="replied",
                    is_read=True,
                    raw_data={
                        "type": "outgoing",
                        "source": "ai_auto_reply",
                        "reply_to_email_id": email_obj.id,
                        "sent_from": from_addr,
                    },
                )
            except Exception as _e:
                # Never let logging failure abort the user-visible flow
                print("AI AUTO-REPLY: could not persist outgoing message:", _e)
        except Exception as e:
            print("AI AUTO-SEND failed, falling back to draft:", e)
            email_obj.status = "drafted"
    else:
        email_obj.status = "drafted"

    return result


def generate_smart_suggestion(customer_body, thread_history=""):
    """Keyword-based tone detection + template reply."""
    text = (body := customer_body or "").lower()

    CANCEL   = ["cancel", "cancellation", "cancel my order"]
    REFUND   = ["refund", "money back", "return", "reimburse", "compensation"]
    TRACKING = ["tracking", "track my", "where is my order", "where is my package",
                "when will", "shipping", "delivery", "shipped", "arrive", "dispatch", "tracking id"]
    ANGRY    = ["angry", "furious", "unacceptable", "terrible", "horrible", "worst",
                "ridiculous", "disgusting", "outraged", "lawsuit", "scam", "fraud", "cheat"]
    FRUSTRATED = ["frustrated", "disappointed", "still waiting", "no response", "again",
                  "fed up", "useless", "sick of", "keep waiting", "nothing happened", "ignored"]
    URGENT   = ["urgent", "asap", "immediately", "emergency", "critical", "right now",
                "today", "must", "deadline", "as soon as possible"]
    CONFUSED = ["confused", "don't understand", "not sure", "unclear", "how do i", "what is",
                "explain", "help me understand", "not working", "doesn't work"]
    POLITE   = ["please", "thank you", "thanks", "appreciate", "kindly",
                "wonderful", "love your", "excellent", "great service"]

    def hit(kws): return any(k in text for k in kws)

    if hit(CANCEL):
        return {"tone": "urgent", "tone_emoji": "⚠️",
                "reply": "Dear Customer,\n\nThank you for reaching out. We have received your cancellation request and our team is reviewing your order status.\n\nIf your order has not been shipped yet, we will proceed with the cancellation and issue a full refund within 24 hours.\n\nBest regards,\nSupport Team"}

    if hit(REFUND):
        return {"tone": "frustrated", "tone_emoji": "😟",
                "reply": "Dear Customer,\n\nWe sincerely apologize for the inconvenience. We have received your refund request and our team is reviewing it as a priority.\n\nRefunds are typically processed within 3–5 business days once approved. We will keep you updated.\n\nBest regards,\nSupport Team"}

    if hit(TRACKING):
        return {"tone": "urgent", "tone_emoji": "📦",
                "reply": "Dear Customer,\n\nThank you for reaching out about your order! We apologize for any delay in communication.\n\nOur team is pulling up your order details right now and will share your tracking information within a few hours.\n\nBest regards,\nSupport Team"}

    if hit(ANGRY):
        return {"tone": "angry", "tone_emoji": "😤",
                "reply": "Dear Customer,\n\nWe sincerely apologize for the experience you have had. This is not the standard we hold ourselves to and we completely understand your frustration.\n\nYour case has been escalated to our senior support team as top priority. We will follow up with you very shortly.\n\nWarm regards,\nSupport Team"}

    if hit(FRUSTRATED):
        return {"tone": "frustrated", "tone_emoji": "😟",
                "reply": "Dear Customer,\n\nWe completely understand your frustration and we are truly sorry for the inconvenience caused.\n\nOur team is actively working on your case right now and will have a resolution for you shortly. Thank you for your patience.\n\nBest regards,\nSupport Team"}

    if hit(URGENT):
        return {"tone": "urgent", "tone_emoji": "⚠️",
                "reply": "Dear Customer,\n\nThank you for contacting us. We understand this is time-sensitive and are treating it as high priority.\n\nOur team is on it right now and will get back to you with a resolution as quickly as possible.\n\nBest regards,\nSupport Team"}

    if hit(CONFUSED):
        return {"tone": "confused", "tone_emoji": "🤔",
                "reply": "Dear Customer,\n\nThank you for reaching out! We are happy to help clarify things for you.\n\nCould you please share a bit more detail so we can guide you in the right direction? Our team is here every step of the way.\n\nBest regards,\nSupport Team"}

    if hit(POLITE):
        return {"tone": "polite", "tone_emoji": "😊",
                "reply": "Dear Customer,\n\nThank you so much for your kind message! We truly appreciate you reaching out.\n\nWe are happy to assist you and will look into your request right away.\n\nWarm regards,\nSupport Team"}

    return {"tone": "neutral", "tone_emoji": "💬",
            "reply": "Dear Customer,\n\nThank you for contacting us. We have received your message and our team is reviewing it carefully.\n\nWe will get back to you with an update as soon as possible.\n\nBest regards,\nSupport Team"}


def generate_auto_draft(email_obj):
    name = email_obj.sender_name or "there"

    return f"""Hello {name},

Thank you for contacting us. We have received your message and our team is checking it.

Best regards,
Support Team"""


SAMPLE_TEMPLATE_DATA = {
    'customer_name': 'Ahmed Khan',
    'customer_email': 'ahmed@example.com',
    'customer_phone': '+92 300 1234567',
    'order_id': '#10248',
    'order_date': 'May 1, 2026',
    'order_items': '2× Premium T-Shirt, 1× Watch',
    'store_name': 'VendorFlow Store',
    'store_url': 'https://store.example.com',
    'store_logo': '',
    'product_image': 'https://placehold.co/200x200/f1f5f9/64748b?text=Product',
    'subtotal': '$109.99',
    'discount_code': 'SAVE10',
    'discount_amount': '$10.00',
    'shipping_amount': '$5.00',
    'tax_amount': '$10.00',
    'order_total': '$124.99',
    # Address
    'shipping_address': '123 Main Street, Apt 4B',
    'shipping_city': 'Karachi',
    'shipping_state': 'Sindh',
    'shipping_postcode': '75500',
    'shipping_country': 'Pakistan',
    'shipping_full_address': '123 Main Street, Apt 4B, Karachi, Sindh 75500, Pakistan',
    # Payment
    'payment_method': 'Credit Card',
    'payment_status': 'Paid',
    # Store sender
    'store_email': 'support@store.example.com',
    # Tracking
    'tracking_number': 'TRK-1234567890',
    'tracking_company': 'DHL Express',
    'tracking_link': '#',
    'tracking_url': '#',
    'tracking_id_only': 'TRK-1234567890',
}


def _build_items_html(line_items, currency='USD'):
    """Build HTML rows for {{order_items}} — one row per product with image, name, attrs, qty, price."""
    SYMBOLS = {'USD': '$', 'EUR': '€', 'GBP': '£', 'CAD': 'CA$', 'AUD': 'A$'}
    sym = SYMBOLS.get(currency, '')
    suffix = f' {currency}' if not sym else ''

    def fmt(v):
        try:
            return f"{sym}{float(v):.2f}{suffix}"
        except Exception:
            return ''

    rows = []
    for li in line_items:
        name = li.get('name') or li.get('title') or 'Item'
        qty = li.get('quantity', 1)

        # Price: prefer subtotal (before discount), fall back to price * qty
        price_raw = li.get('subtotal') or li.get('line_price') or li.get('price', 0)
        try:
            price = float(price_raw)
        except Exception:
            price = 0

        # Product image — WooCommerce: li['image']['src'] | Shopify: li['image_url']
        img_src = ''
        if isinstance(li.get('image'), dict):
            img_src = li['image'].get('src') or ''
        if not img_src:
            img_src = li.get('image_url') or ''
        if not img_src and isinstance(li.get('featured_image'), dict):
            img_src = li['featured_image'].get('url') or ''

        # Variant / attributes — WooCommerce meta_data, Shopify variant_title
        attrs = []
        for m in (li.get('meta_data') or []):
            key = m.get('display_key') or m.get('key') or ''
            val = m.get('display_value') or m.get('value') or ''
            if key and val and not key.startswith('_'):
                attrs.append(f"{key}: {val}")
        variant = li.get('variant_title') or ''
        if variant and variant.lower() not in ('default title', 'default'):
            attrs.insert(0, variant)
        attrs_str = ' · '.join(attrs) if attrs else ''

        img_html = (
            f'<img src="{img_src}" width="72" height="72" '
            f'style="display:block;width:72px;height:72px;object-fit:cover;border-radius:6px;background:#f3f4f6;" />'
            if img_src else
            '<div style="width:72px;height:72px;background:#f3f4f6;border-radius:6px;'
            'display:flex;align-items:center;justify-content:center;">'
            '<span style="font-size:22px;">📦</span></div>'
        )

        price_html = fmt(price) if price else ''

        rows.append(f"""<table width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #e5e7eb;">
<tr>
  <td style="padding:14px 0;vertical-align:top;">
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td width="72" style="vertical-align:top;padding-right:14px;">{img_html}</td>
      <td style="vertical-align:top;">
        <p style="margin:0 0 3px;font-size:14px;font-weight:600;color:#1a1a1a;">{name}</p>
        {'<p style="margin:0 0 3px;font-size:12px;color:#6b7280;">' + attrs_str + '</p>' if attrs_str else ''}
        <p style="margin:0;font-size:12px;color:#6b7280;">Qty: {qty}</p>
      </td>
      <td style="vertical-align:top;text-align:right;white-space:nowrap;padding-left:12px;">
        {'<p style="margin:0;font-size:14px;font-weight:600;color:#1a1a1a;">' + price_html + '</p>' if price_html else ''}
      </td>
    </tr>
    </table>
  </td>
</tr>
</table>""")

    return '\n'.join(rows)


def build_template_context(store, order=None):
    """Return a render context for a store. Pass `order` to use that specific order's data."""
    from urllib.parse import urlparse
    from orders.models import Order as StoreOrder

    ctx = dict(SAMPLE_TEMPLATE_DATA)

    if not store:
        return ctx

    store_url = store.store_url or ''
    store_domain = urlparse(store_url).netloc or store_url
    store_logo_url = (
        f"https://t3.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON"
        f"&fallback_opts=TYPE,SIZE,URL&url={store_url}&size=128"
    )
    from .models import EmailAccount
    account = EmailAccount.objects.filter(store=store, is_active=True).first()
    store_email = account.email if account else ctx.get('store_email', '')

    ctx.update({
        'store_name': store.name,
        'store_url': store_url,
        'store_logo': store_logo_url,
        'store_domain': store_domain,
        'store_email': store_email,
    })

    if order is None:
        order = StoreOrder.objects.filter(store=store).order_by('-created_at').first()
    if not order:
        return ctx

    raw = order.raw_data or {}
    line_items = raw.get('line_items') or []
    if line_items:
        items_str = _build_items_html(line_items, order.currency or 'USD')
    elif order.product_name:
        items_str = _build_items_html([{
            'name': order.product_name, 'quantity': 1, 'subtotal': str(order.total_price or 0)
        }], order.currency or 'USD')
    else:
        items_str = ctx['order_items']

    currency = order.currency or 'USD'
    total = float(order.total_price or 0)

    # WooCommerce: sum line item subtotals (before discount) for subtotal
    items_subtotal = sum(float(li.get('subtotal') or 0) for li in line_items)
    subtotal = float(
        raw.get('subtotal_price') or raw.get('subtotal') or
        (items_subtotal if items_subtotal else None) or total
    )

    shipping_set = raw.get('total_shipping_price_set')
    if isinstance(shipping_set, dict) and 'shop_money' in shipping_set:
        shipping = float(shipping_set['shop_money'].get('amount') or 0)
    else:
        shipping = float(raw.get('shipping_amount') or raw.get('total_shipping') or raw.get('shipping_total') or 0)

    tax = float(raw.get('total_tax') or 0)
    discount = float(raw.get('discount_total') or raw.get('total_discounts') or 0)

    discount_code = ''
    disc_apps = raw.get('discount_applications') or raw.get('discount_codes') or []
    if disc_apps and isinstance(disc_apps, list):
        discount_code = disc_apps[0].get('code') or disc_apps[0].get('title') or ''

    SYMBOLS = {'USD': '$', 'EUR': '€', 'GBP': '£', 'CAD': 'CA$', 'AUD': 'A$'}
    sym = SYMBOLS.get(currency, '')
    suffix = '' if sym else f' {currency}'

    def fmt(v):
        return f"{sym}{v:.2f}{suffix}"

    order_date = order.created_at.strftime('%B %d, %Y').replace(' 0', ' ')

    # Shipping address — support both Shopify and WooCommerce raw_data layouts
    ship = raw.get('shipping_address') or raw.get('shipping') or {}
    bill = raw.get('billing_address') or raw.get('billing') or {}
    addr = ship if (ship.get('address_1') or ship.get('address1')) else bill

    ship_line1 = addr.get('address_1') or addr.get('address1') or addr.get('address') or ''
    ship_line2 = addr.get('address_2') or addr.get('address2') or ''
    ship_city   = addr.get('city') or order.city or ''
    ship_state  = addr.get('state') or addr.get('province') or ''
    ship_post   = addr.get('postcode') or addr.get('zip') or ''
    ship_country = addr.get('country') or order.country or ''

    full_addr_parts = [p for p in [ship_line1, ship_line2, ship_city, ship_state, ship_post, ship_country] if p]
    full_addr = ', '.join(full_addr_parts)

    # Payment
    pay_method = (
        raw.get('payment_method_title') or raw.get('payment_gateway') or
        (raw.get('payment_gateway_names') or [''])[0] if isinstance(raw.get('payment_gateway_names'), list) else ''
    ) or ctx['payment_method']
    pay_status = (
        raw.get('financial_status') or raw.get('payment_status') or
        order.payment_status or ctx['payment_status']
    )
    pay_status = str(pay_status).replace('_', ' ').title() if pay_status else ctx['payment_status']

    # Customer email from billing if not on order
    cust_email = order.customer_email or bill.get('email') or ctx['customer_email']
    cust_phone = order.customer_phone or bill.get('phone') or ship.get('phone') or ctx['customer_phone']

    # Tracking info — latest approved, or latest pending if none approved
    tracking_number = order.tracking_number or ''
    tracking_company = order.tracking_company or ''
    tracking_link = order.tracking_url or ''
    try:
        from vendors.models import VendorTrackingSubmission
        from orders.services import COURIER_URL_TEMPLATES
        sub = (
            VendorTrackingSubmission.objects.filter(order=order, status='approved').order_by('-submitted_at').first()
            or VendorTrackingSubmission.objects.filter(order=order).order_by('-submitted_at').first()
        )
        if sub:
            tracking_number = sub.tracking_number or tracking_number
            tracking_company = sub.courier_name or tracking_company
            # Always build proper URL for known couriers
            if sub.courier_name:
                tmpl = COURIER_URL_TEMPLATES.get(sub.courier_name.lower())
                tracking_link = tmpl.format(num=sub.tracking_number) if tmpl else (sub.tracking_url or tracking_link)
            else:
                tracking_link = sub.tracking_url or tracking_link
    except Exception:
        pass

    # No real tracking data — use sample values so the preview section is visible
    if not tracking_number and not tracking_link:
        tracking_number = ctx.get('tracking_number', 'TRK-1234567890')
        tracking_company = ctx.get('tracking_company', 'DHL Express')

    ctx.update({
        'customer_name': order.customer_name or ctx['customer_name'],
        'customer_email': cust_email,
        'customer_phone': cust_phone,
        'order_id': f"#{order.external_order_id}",
        'order_date': order_date,
        'order_items': items_str,
        'order_total': fmt(total),
        'subtotal': fmt(subtotal),
        'shipping_amount': fmt(shipping) if shipping else '',
        'tax_amount': fmt(tax) if tax else '',
        'discount_amount': fmt(discount) if discount else '',
        'discount_code': discount_code,
        'shipping_address': ship_line1,
        'shipping_city': ship_city,
        'shipping_state': ship_state,
        'shipping_postcode': ship_post,
        'shipping_country': ship_country,
        'shipping_full_address': full_addr,
        'payment_method': pay_method,
        'payment_status': pay_status,
        'tracking_number': tracking_number,
        'tracking_company': tracking_company,
        'tracking_link': tracking_link,
        'tracking_url': tracking_link,  # alias — templates may use either name
        # Only set when there's no link — used in templates to show ID-only block
        'tracking_id_only': tracking_number if (tracking_number and not tracking_link) else '',
    })

    return ctx


def render_template_content(text, context=None):
    """Replace {{variable}}, {{variable|fallback}}, and {{#if var}}...{{/if}} blocks."""
    if not text:
        return ''
    if context is None:
        context = SAMPLE_TEMPLATE_DATA

    # Process {{#if var}}...{{/if}} conditional blocks (innermost first, handles nesting)
    if_pattern = re.compile(r'\{\{#if\s+(\w+)\}\}(.*?)\{\{/if\}\}', re.DOTALL)

    def process_if(match):
        var_name = match.group(1).strip()
        inner = match.group(2)
        value = context.get(var_name, '')
        if value and str(value).strip() not in ('', '0', '0.00', 'None', 'false', 'False'):
            return render_template_content(inner, context)
        return ''

    prev = None
    while prev != text:
        prev = text
        text = if_pattern.sub(process_if, text)

    def replacer(match):
        expr = match.group(1).strip()
        parts = expr.split('|', 1)
        key = parts[0].strip()
        fallback = parts[1].strip().strip('"\'') if len(parts) > 1 else ''
        return str(context.get(key, fallback))

    return re.sub(r'\{\{([^}#/][^}]*)\}\}', replacer, text)


def _imap_connect(account):
    """Connect to IMAP using app password or OAuth2 XOAUTH2."""
    mail = imaplib.IMAP4_SSL(account.imap_host, account.imap_port)
    if getattr(account, 'auth_type', 'password') == 'oauth' and account.oauth_refresh_token:
        client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '') or os.getenv("GOOGLE_CLIENT_ID", "")
        client_secret = getattr(settings, 'GOOGLE_CLIENT_SECRET', '') or os.getenv("GOOGLE_CLIENT_SECRET", "")
        resp = _requests.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "refresh_token",
            "refresh_token": account.oauth_refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }, timeout=15)
        token_data = resp.json()
        access_token = token_data.get("access_token", "")
        if not access_token:
            raise Exception(f"OAuth token refresh failed: {token_data.get('error', 'unknown')} - {token_data.get('error_description', '')}")
        auth_string = f"user={account.email}\x01auth=Bearer {access_token}\x01\x01"
        mail.authenticate("XOAUTH2", lambda x: auth_string.encode())
    else:
        mail.login(account.email, account.app_password)
    return mail


def mark_email_read_in_gmail(account, gmail_uid):
    """Mark a specific email as read (\Seen) in Gmail via IMAP."""
    try:
        mail = _imap_connect(account)
        mail.select(account.sync_folder or "INBOX")
        mail.store(str(gmail_uid).encode(), "+FLAGS", "\\Seen")
        mail.logout()
    except Exception as e:
        print(f"Gmail mark-read error: {e}")


def _gmail_api_get_access_token(account):
    """Refresh and return a Gmail API access token for an OAuth account."""
    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '') or os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = getattr(settings, 'GOOGLE_CLIENT_SECRET', '') or os.getenv("GOOGLE_CLIENT_SECRET", "")
    resp = _requests.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "refresh_token",
        "refresh_token": account.oauth_refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }, timeout=15)
    data = resp.json()
    access_token = data.get("access_token", "")
    if not access_token:
        raise Exception(f"Token refresh failed: {data.get('error', 'unknown')}")
    return access_token


def _sync_gmail_api(account, store):
    """Sync inbox using Gmail HTTP API (for OAuth accounts)."""
    import base64
    from email import message_from_bytes

    access_token = _gmail_api_get_access_token(account)
    headers = {"Authorization": f"Bearer {access_token}"}
    base_url = "https://gmail.googleapis.com/gmail/v1/users/me"
    fetch_limit = max(10, min(account.fetch_limit or 30, 100))

    # List messages in INBOX
    list_resp = _requests.get(
        f"{base_url}/messages",
        headers=headers,
        params={"labelIds": "INBOX", "maxResults": fetch_limit},
        timeout=15,
    )
    if not list_resp.ok:
        raise Exception(f"Gmail API list failed: {list_resp.text[:200]}")

    messages = list_resp.json().get("messages", [])
    saved_count = 0

    for msg_ref in messages:
        msg_id = msg_ref["id"]

        # Check already saved (use gmail_uid = message id)
        if EmailMessage.objects.filter(store=store, gmail_uid=msg_id).exists():
            continue

        # Fetch full message in RAW format
        msg_resp = _requests.get(
            f"{base_url}/messages/{msg_id}",
            headers=headers,
            params={"format": "raw"},
            timeout=15,
        )
        if not msg_resp.ok:
            continue

        msg_data = msg_resp.json()
        raw_b64 = msg_data.get("raw", "")
        raw_bytes = base64.urlsafe_b64decode(raw_b64 + "==")

        label_ids = msg_data.get("labelIds", [])
        is_read = "UNREAD" not in label_ids

        msg = message_from_bytes(raw_bytes)
        subject = clean_text(msg.get("Subject"))
        sender = clean_text(msg.get("From"))
        recipient = clean_text(msg.get("To")) or account.email
        body = extract_body(msg)
        body_html = extract_body_html(msg)

        category = classify_email(subject, body)
        linked_order = find_order_from_email(store, subject, body)

        # Capture RFC Message-ID + Gmail threadId so AI auto-reply can thread properly
        rfc_message_id = (msg.get("Message-ID") or "").strip()
        gmail_thread_id = msg_data.get("threadId") or ""

        email_obj = EmailMessage.objects.create(
            store=store,
            order=linked_order,
            sender=sender,
            recipient=recipient,
            subject=subject,
            body=body,
            body_html=body_html,
            status="drafted",
            category=category,
            is_read=is_read,
            gmail_uid=msg_id,
            raw_data={
                "source": "gmail_api",
                "connected_email": account.email,
                "gmail_uid": msg_id,
                "is_read": is_read,
                "message_id": rfc_message_id,
                "thread_id": gmail_thread_id,
                "references": (msg.get("References") or "").strip(),
            }
        )

        save_attachments(email_obj, msg)

        process_ai_reply_mode(email_obj, account)
        email_obj.save()

        saved_count += 1

    account.last_synced = timezone.now()
    account.save(update_fields=["last_synced"])
    return saved_count


def sync_gmail_inbox(store_id=2):
    store = Store.objects.filter(id=store_id).first()

    if not store:
        return {
            "success": False,
            "message": "Store not found.",
            "count": 0
        }

    accounts = EmailAccount.objects.filter(store=store, is_active=True)

    if not accounts.exists():
        return {
            "success": False,
            "message": "No email connected for this store.",
            "count": 0
        }

    total_saved = 0
    errors = []

    for account in accounts:
        # OAuth accounts use Gmail API instead of IMAP
        if getattr(account, 'auth_type', 'password') == 'oauth' and account.oauth_refresh_token:
            try:
                total_saved += _sync_gmail_api(account, store)
            except Exception as e:
                errors.append(f"{account.email}: {str(e)}")
            continue

        try:
            mail = _imap_connect(account)
            mail.select(account.sync_folder or "INBOX")
            status, messages = mail.search(None, "ALL")

            if status == "OK" and messages and messages[0]:
                fetch_limit = max(10, min(account.fetch_limit or 30, 100))
                email_ids = messages[0].split()[-fetch_limit:]

                for num in email_ids:
                    status, data = mail.fetch(num, "(RFC822 FLAGS)")
                    if status != "OK" or not data or not data[0]:
                        continue

                    flags_data = data[0][0] if isinstance(data[0], tuple) else data[0]
                    is_read = b"\\Seen" in flags_data
                    msg = email.message_from_bytes(data[0][1])

                    subject = clean_text(msg.get("Subject"))
                    sender = clean_text(msg.get("From"))
                    recipient = clean_text(msg.get("To")) or account.email
                    body = extract_body(msg)
                    body_html = extract_body_html(msg)
                    category = classify_email(subject, body)
                    linked_order = find_order_from_email(store, subject, body)
                    gmail_uid = str(num.decode() if isinstance(num, bytes) else num) + "_" + account.email

                    if not EmailMessage.objects.filter(store=store, gmail_uid=gmail_uid).exists():
                        rfc_message_id = (msg.get("Message-ID") or "").strip()
                        email_obj = EmailMessage.objects.create(
                            store=store,
                            order=linked_order,
                            sender=sender,
                            recipient=recipient,
                            subject=subject,
                            body=body,
                            body_html=body_html,
                            status="drafted",
                            category=category,
                            is_read=is_read,
                            gmail_uid=gmail_uid,
                            raw_data={
                                "source": "gmail_imap",
                                "mailbox": "INBOX",
                                "connected_email": account.email,
                                "gmail_uid": gmail_uid,
                                "is_read": is_read,
                                "message_id": rfc_message_id,
                                "references": (msg.get("References") or "").strip(),
                            }
                        )
                        save_attachments(email_obj, msg)
                        process_ai_reply_mode(email_obj, account)
                        email_obj.save()
                        total_saved += 1
                    else:
                        EmailMessage.objects.filter(store=store, gmail_uid=gmail_uid).update(is_read=is_read)

            mail.logout()
            account.last_synced = timezone.now()
            account.save(update_fields=["last_synced"])
        except Exception as e:
            errors.append(f"{account.email}: {str(e)}")

    if errors and total_saved == 0:
        return {"success": False, "message": "; ".join(errors), "count": 0}

    return {
        "success": True,
        "message": f"Inbox synced: {total_saved} new emails" + (f" (errors: {'; '.join(errors)})" if errors else ""),
        "count": total_saved
    }
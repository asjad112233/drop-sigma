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
    Returns dict {orders: [...], notes: [...]}
    """
    found_orders = []
    notes = []
    seen_order_ids = set()

    def _add_order(o):
        if o.id in seen_order_ids:
            return
        seen_order_ids.add(o.id)
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

    # Also try sender email as a customer email match
    if sender_email:
        # Extract clean email if it's "Name <email>"
        m = re.search(r"<([^>]+)>", sender_email)
        clean = (m.group(1) if m else sender_email).strip().lower()
        if clean and "@" in clean:
            try:
                for o in qs_base.filter(customer_email__iexact=clean).order_by("-created_at")[:3]:
                    _add_order(o)
            except Exception as e:
                notes.append(f"sender lookup error: {e}")

    return {"orders": found_orders[:5], "notes": notes}


def serialize_order_for_ai(o):
    """Compact representation of an Order for AI prompt context + UI display."""
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
    }


def build_context_block_for_prompt(orders):
    """Turn list of Order objects into a text block suitable for system prompt."""
    if not orders:
        return ""
    lines = ["DETECTED CUSTOMER CONTEXT (real data from your store):"]
    for o in orders:
        s = serialize_order_for_ai(o)
        line = f"- Order #{s['order_number']}"
        if s["customer_name"]:           line += f" · Customer: {s['customer_name']}"
        if s["product"]:                 line += f" · Product: {s['product']}"
        if s["total"]:                   line += f" · Total: {s['total']}"
        if s["payment_status"]:          line += f" · Payment: {s['payment_status']}"
        if s["fulfillment_status"]:      line += f" · Fulfillment: {s['fulfillment_status']}"
        if s["live_tracking_status"]:    line += f" · Live status: {s['live_tracking_status']}"
        elif s["tracking_status"]:       line += f" · Status: {s['tracking_status']}"
        if s["tracking_number"]:         line += f" · Tracking #: {s['tracking_number']} ({s['tracking_company']})"
        if s["tracking_url"]:            line += f" · Tracking URL: {s['tracking_url']}"
        if s["delivered_at"]:            line += f" · Delivered: {s['delivered_at'][:10]}"
        if s["city"] or s["country"]:    line += f" · Location: {s['city']} {s['country']}".strip()
        lines.append(line)
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


def build_training_system_prompt(profile, snippets, detected_context=""):
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

    # Strong formatting + brevity rules — these are the most important part of the prompt.
    system += (
        "\n\nSTRICT REPLY RULES (must follow):"
        "\n1. Be SHORT and to the point. No filler, no over-explaining."
        "\n2. Open with a brief greeting line (e.g., 'Hi <Name>,'). Use the customer's first name if known."
        "\n3. Body: one focused short paragraph that answers the question directly."
        "\n4. Use a BLANK LINE between the greeting, the body, and the sign-off — this is non-negotiable for readability."
        "\n5. Skip generic phrases like 'Thanks for reaching out!', 'I hope this helps', 'Let me know if you have any other questions' unless they genuinely add value."
        "\n6. Never invent order numbers, tracking, statuses, or dates — use ONLY the DETECTED CUSTOMER CONTEXT block (if present). If data is missing, ask for it."
        "\n7. Sound human and confident, not robotic. Match the brand voice example above."
        "\n8. Output the reply as plain text only — no HTML, no markdown, no subject line."
    )
    return system


def generate_ai_reply(email_obj, account=None):
    """
    Generate an AI reply for a customer email using:
      1. AiTrainingProfile (per store) — brand, tone, language, voice
      2. KnowledgeSnippets  (per store) — FAQs, policies
      3. Auto-detected customer refs   — order #s, emails, tracking
      4. Resolved Order data           — real status, tracking, customer info
    Falls back to per-EmailAccount settings if no profile is set.
    """
    # Lookup tenant store via email_obj
    store = getattr(email_obj, 'store', None)

    # Try to load the per-store training profile + snippets
    profile = _load_training_profile(store)
    snippets = _load_snippets(store)

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

    # ── Path A: New training-profile flow ──────────────────────────────────
    if profile or snippets:
        system_prompt = build_training_system_prompt(profile, snippets, detected_block)

        # Compose the user-side message (subject + body)
        subj = (getattr(email_obj, 'subject', '') or '').strip()
        body = (getattr(email_obj, 'body', '') or '').strip()
        user_msg = (f"Subject: {subj}\n\n{body}" if subj else body) or "(empty email)"

        try:
            reply = call_claude(prompt=user_msg, system=system_prompt, max_tokens=1024)
        except Exception as e:
            print("AI REPLY ERROR (training-profile flow):", str(e))
            reply = generate_auto_draft(email_obj)

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

    prompt = f"""Customer message:
{email_obj.body}{order_context}{custom_block}

Write a {tone} ecommerce support reply in {language}.

STRICT REPLY RULES (must follow):
1. SHORT and to the point — 2 to 3 short sentences max (under 60 words total).
2. Start with a brief greeting on its own line (e.g., "Hi <Name>,").
3. Leave a BLANK LINE between greeting, body, and sign-off — this is required for readability.
4. Skip filler phrases like "Thanks for reaching out!", "I hope this helps", "Let me know if you need anything else" unless they genuinely add value.
5. NEVER invent order numbers, statuses, or tracking — use ONLY the Order context block if present. If data is missing, ask for it.
6. Sound human and professional, not robotic.

Plain text only — no HTML, no markdown, no subject line. The sign-off will be appended separately."""

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
    result = {"mode_used": mode, "sent": False, "draft": ""}

    # Off → quick keyword draft only
    if mode == "off":
        email_obj.ai_draft = quick_reply(email_obj.body) or generate_auto_draft(email_obj)
        result["draft"] = email_obj.ai_draft
        return result

    # Generate the AI draft (used by all other modes)
    try:
        draft = generate_ai_reply(email_obj, account)
    except Exception as e:
        print("AI REPLY MODE — generation failed:", e)
        draft = quick_reply(email_obj.body) or generate_auto_draft(email_obj)

    email_obj.ai_draft = draft
    result["draft"] = draft

    # Decide whether to auto-send
    should_send = False
    if mode == "auto":
        should_send = True
    elif mode == "hybrid" and _is_simple_email(email_obj.body):
        should_send = True

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
import re
import imaplib
import email

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


def generate_ai_reply(email_obj, account=None):
    tone_map = {
        "formal": "Formal",
        "friendly": "Friendly & Professional",
        "concise": "Concise",
        "empathetic": "Empathetic",
    }
    lang_map = {
        "english": "English",
        "arabic": "Arabic",
        "urdu": "Urdu",
        "french": "French",
    }

    tone = "Friendly & Professional"
    language = "English"
    include_order = True

    if account:
        tone = tone_map.get(account.ai_tone, "Friendly & Professional")
        language = lang_map.get(account.ai_language, "English")
        include_order = account.ai_include_order

    order_context = ""
    if include_order and getattr(email_obj, "order", None):
        o = email_obj.order
        order_context = f"\n\nOrder context: #{getattr(o, 'external_order_id', o.id)}, Status: {getattr(o, 'status', 'unknown')}"

    prompt = f"""Customer message:
{email_obj.body}{order_context}

Write a {tone} ecommerce support reply in {language} (3-5 lines). Be human and empathetic.
- If tracking/shipping question → mention we will share tracking details
- If refund request → acknowledge and explain next steps
- If angry/frustrated → sincerely apologize first
- Keep it concise and warm

Reply only, no subject line, no extra formatting."""

    try:
        return call_claude(prompt)
    except Exception as e:
        print("AI REPLY ERROR:", str(e))
        return generate_auto_draft(email_obj)


def generate_ai_text(prompt):
    try:
        return call_claude(prompt)
    except Exception as e:
        print("AI TEXT ERROR:", str(e))
        return None


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


def mark_email_read_in_gmail(account, gmail_uid):
    """Mark a specific email as read (\Seen) in Gmail via IMAP."""
    try:
        mail = imaplib.IMAP4_SSL(account.imap_host, account.imap_port)
        mail.login(account.email, account.app_password)
        mail.select(account.sync_folder or "INBOX")
        mail.store(str(gmail_uid).encode(), "+FLAGS", "\\Seen")
        mail.logout()
    except Exception as e:
        print(f"Gmail mark-read error: {e}")


def sync_gmail_inbox(store_id=2):
    store = Store.objects.filter(id=store_id).first()

    if not store:
        return {
            "success": False,
            "message": "Store not found.",
            "count": 0
        }

    account = EmailAccount.objects.filter(store=store, is_active=True).first()

    if not account:
        return {
            "success": False,
            "message": "No email connected for this store.",
            "count": 0
        }

    try:
        mail = imaplib.IMAP4_SSL(account.imap_host, account.imap_port)
        mail.login(account.email, account.app_password)
        mail.select(account.sync_folder or "INBOX")

        status, messages = mail.search(None, "ALL")

        if status != "OK" or not messages or not messages[0]:
            mail.logout()
            account.last_synced = timezone.now()
            account.save(update_fields=["last_synced"])
            return {
                "success": True,
                "message": "Inbox synced: 0 new emails",
                "count": 0
            }

        fetch_limit = max(10, min(account.fetch_limit or 30, 100))
        email_ids = messages[0].split()[-fetch_limit:]
        saved_count = 0

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

            category = classify_email(subject, body)
            linked_order = find_order_from_email(store, subject, body)
            gmail_uid = str(num.decode() if isinstance(num, bytes) else num)

            exists = EmailMessage.objects.filter(
                store=store,
                gmail_uid=gmail_uid,
                recipient=recipient
            ).exists()

            if not exists:
                email_obj = EmailMessage.objects.create(
                    store=store,
                    order=linked_order,
                    sender=sender,
                    recipient=recipient,
                    subject=subject,
                    body=body,
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
                    }
                )

                save_attachments(email_obj, msg)

                if account.ai_auto_draft:
                    email_obj.ai_draft = generate_ai_reply(email_obj, account)
                else:
                    email_obj.ai_draft = quick_reply(email_obj.body) or generate_auto_draft(email_obj)
                email_obj.save()

                saved_count += 1

            else:
                EmailMessage.objects.filter(
                    store=store,
                    gmail_uid=gmail_uid,
                    recipient=recipient
                ).update(is_read=is_read)

        mail.logout()

        account.last_synced = timezone.now()
        account.save(update_fields=["last_synced"])

        return {
            "success": True,
            "message": f"Inbox synced: {saved_count} new emails",
            "count": saved_count
        }

    except Exception as e:
        return {
            "success": False,
            "message": str(e),
            "count": 0
        }
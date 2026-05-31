"""Seed a handful of realistic customer emails for local visual testing.

Usage:
    python manage.py seed_sample_emails               # uses first active store
    python manage.py seed_sample_emails --store-id 18
    python manage.py seed_sample_emails --count 12    # how many to create
    python manage.py seed_sample_emails --wipe        # clear seeded ones first

Only touches local data and is safe to re-run — seeded rows are tagged
gmail_uid="seed_*" so they don't collide with real Gmail-synced rows.
"""
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from emails.models import EmailMessage
from stores.models import Store


SAMPLES = [
    {
        "sender_name": "Muhammad Asjad",
        "sender": "asjadakhtaruae@gmail.com",
        "subject": "Re: Shipping Info",
        "body": "ok i checking",
        "category": "shipping",
        "status": "drafted",
        "is_read": False,
        "ai_draft": "Sounds good! Let me know if you have any questions while browsing, or feel free to reach out once you're ready to checkout.",
    },
    {
        "sender_name": "Anmol Ideas",
        "sender": "anmolideasofficial@gmail.com",
        "subject": "Re: Order Confirmation #11247",
        "body": "My Tracking link is not working — when I open it the page just says invalid tracking ID. Order is #11247 placed last Tuesday.",
        "category": "shipping",
        "status": "drafted",
        "is_read": False,
        "ai_draft": "I'm sorry to hear that. I just checked and your tracking number is being re-uploaded by our courier — the link should work within the next 30 minutes. I'll keep an eye on it.",
    },
    {
        "sender_name": "Maisons De Monaco",
        "sender": "maisonsdemonaco@gmail.com",
        "subject": "Where is my order",
        "body": "Hi team, I placed an order four days ago (order #11183) and still haven't received any shipping confirmation. Could you please update me on the status?",
        "category": "shipping",
        "status": "drafted",
        "is_read": True,
        "ai_draft": "Hi! Order #11183 is currently being prepared for shipment. Typical fulfillment for this product is 1–2 business days, so it should ship out today or tomorrow. You'll receive a tracking link by email as soon as it does.",
    },
    {
        "sender_name": "Sarah Kim",
        "sender": "sarah.kim@outlook.com",
        "subject": "Can I change my delivery address?",
        "body": "Hi — I just placed order #11290 but I'd like to ship to a different address. The new address is: 14B North Block, San Francisco, CA 94103. Is that possible?",
        "category": "general",
        "status": "new",
        "is_read": False,
        "ai_draft": "Yes — since your order hasn't shipped yet, we can update the address. I've changed it to 14B North Block, San Francisco, CA 94103 and you'll see a confirmation in your account within a few minutes.",
    },
    {
        "sender_name": "Jordan Rivera",
        "sender": "jordan.rivera@yahoo.com",
        "subject": "Refund request — #11154",
        "body": "Hello, I'd like to request a refund for order #11154. The product arrived but it's the wrong size. I'd like to return it. Please advise on the next steps.",
        "category": "refund",
        "status": "drafted",
        "is_read": False,
        "ai_draft": "I'm sorry about the size mismatch. Returns within 14 days are no problem — I've started the return for #11154 and emailed you a prepaid shipping label. Once we receive the item we'll refund the original payment method within 3–5 business days.",
    },
    {
        "sender_name": "Lina Park",
        "sender": "lina.p@protonmail.com",
        "subject": "Product question",
        "body": "Hey there! Is the IN Glock We Trust Oversized Tee available in size XL? Your website is showing 'out of stock' but I really want to grab one.",
        "category": "product",
        "status": "drafted",
        "is_read": True,
        "ai_draft": "Hey Lina — XL is restocking next Tuesday. If you'd like, I can add you to the notify-on-restock list so you get the link as soon as it's live (we usually sell through XL within the first hour).",
    },
    {
        "sender_name": "Ahmed Faraz",
        "sender": "ahmed.faraz@gmail.com",
        "subject": "Re: Payment failed for #11302",
        "body": "I tried to pay again with the same card and it failed twice. The bank says they don't see any decline on their side. Any idea what's wrong?",
        "category": "payment",
        "status": "new",
        "is_read": False,
        "ai_draft": "Sorry about that — when a card retries quickly twice the gateway sometimes blocks the third attempt. Could you wait 10–15 minutes and try again? If it still fails I'll send you a direct payment link.",
    },
    {
        "sender_name": "Priya Sharma",
        "sender": "priya.sh@gmail.com",
        "subject": "Thank you!",
        "body": "Just wanted to say the hoodie arrived today and it's gorgeous. The quality is amazing — definitely buying more. Thank you for the fast shipping!",
        "category": "general",
        "status": "replied",
        "is_read": True,
        "ai_draft": "",
    },
    {
        "sender_name": "Daniel Wong",
        "sender": "daniel.w@gmail.com",
        "subject": "Order #11176 — wrong color received",
        "body": "Hi, I ordered the In Draco We Trust Oversized Tee in Red but received Black. Can we sort this out? I'd prefer to get the Red one I ordered.",
        "category": "refund",
        "status": "drafted",
        "is_read": False,
        "ai_draft": "Apologies for the mix-up. I've created a replacement order for the Red Oversized Tee — it's going out tomorrow with priority shipping. You can keep the Black one or return it free of charge, your choice.",
    },
    {
        "sender_name": "Fatima Ali",
        "sender": "fatima.ali22@gmail.com",
        "subject": "Want Tracking ID! ORDER #11110",
        "body": "Hello, could you please share the current status of my order #11110? I still have not received the tracking ID, so kindly provide an update and the tracking details as soon as possible.",
        "category": "shipping",
        "status": "drafted",
        "is_read": False,
        "ai_draft": "Hi Fatima — Order #11110 was placed 2 days ago and is currently being prepared for shipment. The tracking link is auto-generated as soon as it ships (usually within 1–2 business days). You'll receive it by email automatically.",
    },
    {
        "sender_name": "Omar Hassan",
        "sender": "omar.h@gmail.com",
        "subject": "Discount code not working",
        "body": "Hey, I'm trying to use the WELCOME10 code at checkout but it says invalid. Has it expired? I really wanted to use it for my order today.",
        "category": "general",
        "status": "drafted",
        "is_read": True,
        "ai_draft": "WELCOME10 is for first-time orders only and it looks like it was used on a previous purchase. I've just generated a fresh 10% code for you — RETURN10 — valid for 7 days on your account.",
    },
    {
        "sender_name": "Aisha Tariq",
        "sender": "aisha.tariq@gmail.com",
        "subject": "Did you receive my return?",
        "body": "I sent back order #10988 about a week ago using the prepaid label. Wondering if it's been received yet and when I'll see the refund?",
        "category": "refund",
        "status": "drafted",
        "is_read": True,
        "ai_draft": "Yes — your return arrived at our warehouse yesterday and is being inspected. Refunds usually process within 24 hours of inspection, so you should see it on your original card within 1–2 business days.",
    },
]


class Command(BaseCommand):
    help = "Seed sample customer emails for local visual testing."

    def add_arguments(self, parser):
        parser.add_argument("--store-id", type=int, default=None,
                            help="Target store. Defaults to the first active store.")
        parser.add_argument("--count", type=int, default=len(SAMPLES),
                            help=f"How many sample emails to insert (max {len(SAMPLES)}).")
        parser.add_argument("--wipe", action="store_true",
                            help="Remove previously seeded sample emails first.")

    def handle(self, *args, **opts):
        store_id = opts.get("store_id")
        if store_id:
            store = Store.objects.filter(id=store_id).first()
        else:
            store = Store.objects.filter(is_active=True).order_by("id").first()
            if not store:
                store = Store.objects.order_by("id").first()
        if not store:
            self.stderr.write(self.style.ERROR("No store found. Create one first."))
            return

        if opts.get("wipe"):
            removed = EmailMessage.objects.filter(
                store=store, gmail_uid__startswith="seed_",
            ).delete()
            self.stdout.write(self.style.WARNING(f"Wiped: {removed}"))

        count = max(1, min(opts.get("count") or len(SAMPLES), len(SAMPLES)))
        now = timezone.now()

        created = 0
        for i, sample in enumerate(SAMPLES[:count]):
            uid = f"seed_{i+1:02d}_{sample['sender']}"
            if EmailMessage.objects.filter(store=store, gmail_uid=uid).exists():
                continue
            # Spread timestamps over the last ~3 days for a realistic inbox.
            ts_offset = timedelta(
                hours=random.randint(0, 72),
                minutes=random.randint(0, 59),
            )
            ts = now - ts_offset
            EmailMessage.objects.create(
                store=store,
                sender=sample["sender"],
                sender_name=sample["sender_name"],
                recipient=store.user.email if getattr(store, "user", None) else "support@dropsigma.com",
                subject=sample["subject"],
                body=sample["body"],
                body_html="",
                category=sample.get("category", "general"),
                status=sample.get("status", "new"),
                is_read=sample.get("is_read", False),
                ai_draft=sample.get("ai_draft", "") or "",
                gmail_uid=uid,
                raw_data={
                    "source": "seed",
                    "seeded_at": now.isoformat(),
                    "thread_id": f"seedthread_{i+1:02d}",
                },
                created_at=ts,
            )
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f"✓ {created} sample email(s) inserted into store '{store.name}' (id={store.id})."
        ))

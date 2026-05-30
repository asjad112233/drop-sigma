"""
Minimal regression tests for the RMA app.

Covers:
- Tenant isolation: a user cannot see / act on another tenant's RMA.
- Status state machine: invalid transitions return 400 (not crash).
- Refund leaves status='refunded' (not 'resolved') so the tab is populated.
- Idempotency: refund a refunded RMA returns success (no crash).
- Customer order-id verification requires email (no order-id-only leak).
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse

from stores.models import Store
from orders.models import Order
from rma.models import RMA, RMAItem, RMASettings


class RMALifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="tenant1", password="pw", is_staff=True, email="t1@x.com",
        )
        self.other = User.objects.create_user(
            username="tenant2", password="pw", is_staff=True, email="t2@x.com",
        )
        self.store = Store.objects.create(
            user=self.user, name="My Store", platform="shopify",
            store_url="https://example.com",
        )
        self.other_store = Store.objects.create(
            user=self.other, name="Other Store", platform="shopify",
            store_url="https://other.example.com",
        )
        self.order = Order.objects.create(
            store=self.store, external_order_id="ORD-1",
            customer_email="buyer@example.com",
            customer_name="Buyer",
            product_name="Widget",
            total_price=Decimal("50.00"),
        )

        self.rma = RMA.objects.create(
            store=self.store, order=self.order,
            rma_number="RMA-2401",
            customer_email="buyer@example.com",
            status="pending",
        )
        RMAItem.objects.create(
            rma=self.rma, product_name="Widget", quantity=1,
            unit_price=Decimal("50.00"),
        )

        self.client = Client()
        self.client.login(username="tenant1", password="pw")

    # --- tenant isolation -------------------------------------------------
    def test_other_tenant_cannot_see_rma(self):
        other_client = Client()
        other_client.login(username="tenant2", password="pw")
        resp = other_client.get(reverse("rma_api_detail", args=[self.rma.id]))
        self.assertEqual(resp.status_code, 404)

    def test_other_tenant_cannot_approve_rma(self):
        other_client = Client()
        other_client.login(username="tenant2", password="pw")
        resp = other_client.post(
            reverse("rma_approve", args=[self.rma.id]),
            data="{}", content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    # --- state machine ----------------------------------------------------
    def test_approve_then_received_then_refund(self):
        r = self.client.post(reverse("rma_approve", args=[self.rma.id]),
                             data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "approved")

        r = self.client.post(reverse("rma_mark_received", args=[self.rma.id]),
                             data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "received")

        r = self.client.post(reverse("rma_refund", args=[self.rma.id]),
                             data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "refunded")

        self.rma.refresh_from_db()
        self.assertEqual(self.rma.status, "refunded")
        self.assertIsNotNone(self.rma.refunded_at)

    def test_cannot_refund_pending(self):
        r = self.client.post(reverse("rma_refund", args=[self.rma.id]),
                             data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_cannot_approve_resolved(self):
        self.rma.status = "resolved"
        self.rma.save()
        r = self.client.post(reverse("rma_approve", args=[self.rma.id]),
                             data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_refund_is_idempotent(self):
        self.client.post(reverse("rma_approve", args=[self.rma.id]),
                         data="{}", content_type="application/json")
        r1 = self.client.post(reverse("rma_refund", args=[self.rma.id]),
                              data="{}", content_type="application/json")
        self.assertEqual(r1.status_code, 200)

        r2 = self.client.post(reverse("rma_refund", args=[self.rma.id]),
                              data="{}", content_type="application/json")
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json().get("noop"))

    def test_bad_decimal_doesnt_crash_approve(self):
        r = self.client.post(
            reverse("rma_approve", args=[self.rma.id]),
            data='{"refund_amount":"not-a-number"}', content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)

    # --- customer-side leak protection -----------------------------------
    def test_cust_start_requires_email(self):
        c = Client()
        resp = c.get(reverse("rma_cust_start", args=["ORD-1"]))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context.get("order"))

    def test_cust_submit_requires_email(self):
        c = Client()
        resp = c.post(reverse("rma_cust_submit", args=["ORD-1"]), data={})
        self.assertEqual(resp.status_code, 400)

"""Tests for the Vendor Pricing & Approval System (v1)."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from stores.models import Store
from vendors.models import (
    Vendor, ProductVendorAssignment,
    VendorQuote, VendorShippingOverride, QuoteChangeRequest,
    VendorTrustSetting, ReasonConfig,
)
from vendors.services import (
    evaluate_change_request, apply_approved_change, compute_deltas,
)


def _make_fixture():
    """Build a tenant/vendor/assignment/active-quote fixture."""
    tenant = User.objects.create_user(username="t1", password="x", email="t1@dropsigma.com")
    store = Store.objects.create(user=tenant, name="Store 1", platform="shopify",
                                 store_url="https://s1.example")
    vendor = Vendor.objects.create(name="V1", email="v1@dropsigma.com",
                                   assigned_store=store)
    assignment = ProductVendorAssignment.objects.create(
        store=store, product_id="p1", product_name="Widget", vendor=vendor,
    )
    quote = VendorQuote.objects.create(
        assignment=assignment, product_cost=Decimal("10"),
        default_shipping=Decimal("5"), status=VendorQuote.STATUS_ACTIVE,
    )
    return tenant, vendor, assignment, quote


def _make_change(quote, change_type, old, new, reason="currency_fluctuation"):
    delta_abs, delta_pct = compute_deltas(old, new)
    return QuoteChangeRequest.objects.create(
        quote=quote, change_type=change_type,
        old_value=Decimal(str(old)), new_value=Decimal(str(new)),
        delta_abs=delta_abs, delta_pct=delta_pct, reason_key=reason,
    )


class AutomationEngineTests(TestCase):
    """Rule 1: cost decrease ALWAYS auto-approves (regardless of trust/threshold)."""

    @classmethod
    def setUpTestData(cls):
        # ReasonConfig seed data is loaded by the migration; ensure it's present
        # in case tests run with --keepdb=off (they always run migrations).
        for key, label, auto in [
            ("currency_fluctuation", "Currency fluctuation", True),
            ("supplier_cost", "Supplier cost changed", False),
        ]:
            ReasonConfig.objects.get_or_create(
                key=key, defaults={"label": label, "auto_eligible": auto}
            )

    def test_cost_decrease_auto_approves_regardless_of_trust(self):
        _t, _v, _a, quote = _make_fixture()
        # Vendor proposes cost DROP: 10 → 8
        change = _make_change(quote, QuoteChangeRequest.CHANGE_PRODUCT_COST, 10, 8)
        decision, reason = evaluate_change_request(change)
        self.assertEqual(decision, "auto_approved")
        self.assertEqual(reason, "cost_decrease")

    def test_no_trust_setting_defaults_to_pending(self):
        _t, _v, _a, quote = _make_fixture()
        # Cost INCREASE 10 → 11, no trust row exists
        change = _make_change(quote, QuoteChangeRequest.CHANGE_PRODUCT_COST, 10, 11)
        decision, reason = evaluate_change_request(change)
        self.assertEqual(decision, "pending")
        self.assertEqual(reason, "trust_off")

    def test_trust_off_defaults_to_pending(self):
        tenant, vendor, _a, quote = _make_fixture()
        VendorTrustSetting.objects.create(tenant=tenant, vendor=vendor, trust_mode=False)
        change = _make_change(quote, QuoteChangeRequest.CHANGE_PRODUCT_COST, 10, 11)
        decision, reason = evaluate_change_request(change)
        self.assertEqual(decision, "pending")
        self.assertEqual(reason, "trust_off")

    def test_trust_on_within_threshold_auto_approves(self):
        tenant, vendor, _a, quote = _make_fixture()
        VendorTrustSetting.objects.create(tenant=tenant, vendor=vendor, trust_mode=True,
                                          threshold_pct=Decimal("10"),
                                          threshold_abs=Decimal("5"))
        # 10 → 10.50 (5% increase, $0.50 abs)
        change = _make_change(quote, QuoteChangeRequest.CHANGE_PRODUCT_COST, 10, Decimal("10.50"))
        decision, reason = evaluate_change_request(change)
        self.assertEqual(decision, "auto_approved")
        self.assertEqual(reason, "within_threshold")

    def test_trust_on_over_pct_threshold(self):
        tenant, vendor, _a, quote = _make_fixture()
        VendorTrustSetting.objects.create(tenant=tenant, vendor=vendor, trust_mode=True)
        # 10 → 12 (20% > 10%)
        change = _make_change(quote, QuoteChangeRequest.CHANGE_PRODUCT_COST, 10, 12)
        decision, reason = evaluate_change_request(change)
        self.assertEqual(decision, "pending")
        self.assertEqual(reason, "over_threshold_pct")

    def test_trust_on_over_abs_threshold(self):
        tenant, vendor, _a, quote = _make_fixture()
        VendorTrustSetting.objects.create(tenant=tenant, vendor=vendor, trust_mode=True,
                                          threshold_pct=Decimal("100"),  # disable pct
                                          threshold_abs=Decimal("5"))
        # 100 → 106 ($6 > $5 even though pct is 6%)
        change = _make_change(quote, QuoteChangeRequest.CHANGE_PRODUCT_COST, 100, 106)
        decision, reason = evaluate_change_request(change)
        self.assertEqual(decision, "pending")
        self.assertEqual(reason, "over_threshold_abs")

    def test_non_auto_eligible_reason_escalates(self):
        tenant, vendor, _a, quote = _make_fixture()
        VendorTrustSetting.objects.create(tenant=tenant, vendor=vendor, trust_mode=True)
        change = _make_change(quote, QuoteChangeRequest.CHANGE_PRODUCT_COST, 10,
                              Decimal("10.50"), reason="supplier_cost")
        decision, reason = evaluate_change_request(change)
        self.assertEqual(decision, "pending")
        self.assertEqual(reason, "reason_not_auto_eligible")


class ApplyApprovedChangeTests(TestCase):
    """apply_approved_change creates a NEW VendorQuote (snapshot) and
    supersedes the old one — original orders stay attached to the old quote."""

    def test_product_cost_change_creates_new_active_quote(self):
        _t, _v, _a, old = _make_fixture()
        VendorShippingOverride.objects.create(
            quote=old, country_code="US", shipping_cost=Decimal("8")
        )
        change = _make_change(old, QuoteChangeRequest.CHANGE_PRODUCT_COST, 10, 12)
        new_quote = apply_approved_change(change)

        old.refresh_from_db()
        self.assertEqual(old.status, VendorQuote.STATUS_SUPERSEDED)
        self.assertEqual(new_quote.status, VendorQuote.STATUS_ACTIVE)
        self.assertEqual(new_quote.product_cost, Decimal("12"))
        self.assertEqual(new_quote.default_shipping, Decimal("5"))
        # Old override copied to new quote
        self.assertTrue(new_quote.overrides.filter(country_code="US").exists())

    def test_country_removed_drops_override_on_new_quote(self):
        _t, _v, _a, old = _make_fixture()
        VendorShippingOverride.objects.create(
            quote=old, country_code="UK", shipping_cost=Decimal("12")
        )
        # Removal change: old=12 (override), new=5 (default)
        change = QuoteChangeRequest.objects.create(
            quote=old, change_type=QuoteChangeRequest.CHANGE_COUNTRY_REMOVED,
            country_code="UK", old_value=Decimal("12"), new_value=Decimal("5"),
            delta_abs=Decimal("-7"), delta_pct=Decimal("-58.33"),
            reason_key="other", notes="region cancelled",
        )
        new_quote = apply_approved_change(change)
        self.assertFalse(new_quote.overrides.filter(country_code="UK").exists())


class ComputeDeltasTests(TestCase):
    def test_zero_old_value_returns_zero_pct(self):
        self.assertEqual(compute_deltas(0, 5), (Decimal("5.00"), Decimal("0.00")))

    def test_basic_increase(self):
        d_abs, d_pct = compute_deltas(10, 11)
        self.assertEqual(d_abs, Decimal("1.00"))
        self.assertEqual(d_pct, Decimal("10.00"))

    def test_decrease_is_negative(self):
        d_abs, d_pct = compute_deltas(10, 8)
        self.assertEqual(d_abs, Decimal("-2.00"))
        self.assertEqual(d_pct, Decimal("-20.00"))

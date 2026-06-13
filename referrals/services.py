"""Referral business logic. Keep model save() free of side-effects;
mutations route through these helpers so signup / payment hooks have
a single, testable surface."""

from __future__ import annotations

import datetime
import secrets

from django.db import transaction
from django.utils import timezone

from .models import ReferralCode, ReferralAttribution, REWARD_THRESHOLD, CODE_LENGTH


_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ambiguity-stripped


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))


def get_or_create_code(user) -> ReferralCode:
    """Idempotent: returns the user's ReferralCode, creating one with a
    collision-safe random code on first call. Retries on the (extremely
    rare) duplicate code race."""
    rc = ReferralCode.objects.filter(user=user).first()
    if rc:
        return rc
    # 5 attempts is more than enough for an alphabet of 32^8.
    for _ in range(5):
        code = _generate_code()
        try:
            return ReferralCode.objects.create(user=user, code=code)
        except Exception:
            continue
    # Final attempt with a slightly longer code as escape hatch.
    return ReferralCode.objects.create(
        user=user, code=_generate_code() + _generate_code()[:2]
    )


def attribute_signup(referrer_user, referred_user, code_snapshot: str = "") -> ReferralAttribution | None:
    """Record that ``referred_user`` signed up via ``referrer_user``'s
    link. Idempotent — a second call is a silent no-op (returns the
    existing row). Returns None if self-referral, missing referrer,
    or any guard fails."""

    if not referrer_user or not referred_user:
        return None
    if referrer_user.id == referred_user.id:
        return None

    existing = ReferralAttribution.objects.filter(referred_user=referred_user).first()
    if existing:
        return existing

    with transaction.atomic():
        att = ReferralAttribution.objects.create(
            referrer=referrer_user,
            referred_user=referred_user,
            code_snapshot=(code_snapshot or "")[:20],
        )
        # Bump the referrer's denormalised total_signups counter.
        ReferralCode.objects.filter(user=referrer_user).update(
            total_signups=models_F() + 1
        )
    return att


def models_F():
    # Lazy import so the module loads cleanly even if Django isn't
    # fully wired (used by attribute_signup / qualify_referral above).
    from django.db.models import F
    return F("total_signups")


def _qualified_F():
    from django.db.models import F
    return F("qualified_signups")


def qualify_referral(referred_user, *, event: str = "subscription_paid",
                     plan: str = "") -> bool:
    """Mark the attribution row for ``referred_user`` as qualified
    (idempotent). If this is the qualifying event that pushes the
    referrer past ``REWARD_THRESHOLD``, also grant the +3 months
    reward.

    Returns True if anything changed (qualified flag freshly set OR
    reward freshly granted), False if everything was already in place.
    """
    if not referred_user:
        return False

    att = (ReferralAttribution.objects
           .select_related("referrer")
           .filter(referred_user=referred_user).first())
    if not att:
        return False

    changed = False
    with transaction.atomic():
        if not att.qualified_at:
            att.qualified_at = timezone.now()
            att.qualified_event = event[:40]
            att.qualified_plan = (plan or "")[:40]
            att.save(update_fields=["qualified_at", "qualified_event", "qualified_plan"])
            ReferralCode.objects.filter(user=att.referrer).update(
                qualified_signups=_qualified_F() + 1
            )
            changed = True
        # Re-read denormalised counter post-update for the reward check.
        rc = ReferralCode.objects.filter(user=att.referrer).first()
        if rc and not rc.reward_granted_at and rc.qualified_signups >= REWARD_THRESHOLD:
            granted = _grant_reward(att.referrer)
            if granted:
                ReferralCode.objects.filter(pk=rc.pk).update(
                    reward_granted_at=timezone.now()
                )
                changed = True
    return changed


def _grant_reward(referrer_user) -> bool:
    """Extend the referrer's subscription by 3 months.

    Touches whichever date fields the project has in place — both
    ``Tenant.trial_ends`` and ``Subscription.renews_on`` /
    ``current_period_end`` if present — so an active paid sub OR a
    still-trialing one both walk forward by the same 90 days.

    Returns True on any extension actually applied, False if the user
    has no Tenant / Subscription rows yet (in which case we leave the
    reward un-granted so the next call after they sign their tenant
    up still applies it).
    """
    try:
        # Local imports — keep this module safe to import from
        # signup_view / verify_email / Stripe webhooks early in boot.
        from superadmin.models import Tenant
    except Exception:
        return False

    tenant = getattr(referrer_user, "tenant_profile", None)
    if not tenant:
        try:
            tenant = Tenant.objects.filter(user=referrer_user).first()
        except Exception:
            tenant = None
    if not tenant:
        return False

    delta = datetime.timedelta(days=90)
    today = datetime.date.today()
    any_change = False

    # Extend trial_ends if it's in the future (still trialing).
    if tenant.trial_ends:
        base = tenant.trial_ends if tenant.trial_ends >= today else today
        tenant.trial_ends = base + delta
        tenant.save(update_fields=["trial_ends"])
        any_change = True

    # Extend Subscription.renews_on AND current_period_end if present.
    sub = getattr(tenant, "subscription", None)
    if sub:
        upd_fields = []
        if sub.renews_on:
            base = sub.renews_on if sub.renews_on >= today else today
            sub.renews_on = base + delta
            upd_fields.append("renews_on")
        if getattr(sub, "current_period_end", None):
            base_dt = sub.current_period_end
            now = timezone.now()
            if base_dt < now:
                base_dt = now
            sub.current_period_end = base_dt + delta
            upd_fields.append("current_period_end")
        if upd_fields:
            sub.save(update_fields=upd_fields)
            any_change = True

    # Audit row so the superadmin sees WHY a subscription jumped 3 months.
    try:
        from superadmin.models import TenantActivity
        TenantActivity.objects.create(
            tenant=tenant,
            action="Referral reward applied — 3 months added (3 qualified signups)",
            action_type="referral_reward",
        )
    except Exception:
        pass

    return any_change


def progress_for(user) -> dict:
    """Read-only view of where the user stands. Used by the dashboard
    poll AND the modal under the Get-my-link button."""
    rc = ReferralCode.objects.filter(user=user).first()
    total = rc.total_signups if rc else 0
    qualified = rc.qualified_signups if rc else 0
    return {
        "code":               rc.code if rc else "",
        "total_signups":      total,
        "qualified_signups":  qualified,
        "target":             REWARD_THRESHOLD,
        "unlocked":           bool(rc and rc.reward_granted_at),
        "reward_granted_at":  rc.reward_granted_at.isoformat() if rc and rc.reward_granted_at else None,
    }

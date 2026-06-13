"""Referral system models.

Two tables:

* ``ReferralCode`` — one per tenant user. Holds their unique short code
  + denormalised counters so the dashboard can read in a single query.

* ``ReferralAttribution`` — every referred signup. Created when a new
  user signs up via ``/invite/<code>/`` AND links the referrer to the
  referred ``User``. Flips to ``qualified=True`` the moment the
  referred user upgrades from trial to a paid subscription, at which
  point the referrer's ``qualified_signups`` increments and — on
  hitting ``REWARD_THRESHOLD`` — three free months are added to the
  referrer's subscription via ``services.grant_reward_if_eligible``.

Nothing here is local-only. The banner in dashboard.html that surfaces
this data is gated to localhost so we can QA before flipping it on for
production; the system itself works in every environment.
"""

from django.db import models
from django.conf import settings


# How many qualified referrals unlock the reward.
REWARD_THRESHOLD = 3
# Length of the public code in /invite/<code>/. 8 chars from url-safe
# alphabet = 32^8 ≈ 1 trillion — collision-free in practice.
CODE_LENGTH = 8


class ReferralCode(models.Model):
    """A tenant's unique invite code. Auto-created on first
    /api/referrals/my-link/ hit so we don't waste rows on accounts
    that never share."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="referral_code",
    )
    code = models.CharField(max_length=20, unique=True, db_index=True)

    # Denormalised counters — kept in sync by services.attribute_signup
    # and services.qualify_referral. Cheaper than a COUNT() on every
    # dashboard poll, and the writes are bounded (one per signup, one
    # per qualifying upgrade).
    total_signups       = models.PositiveIntegerField(default=0)
    qualified_signups   = models.PositiveIntegerField(default=0)

    # Set when ``qualified_signups`` first reaches REWARD_THRESHOLD and
    # the +3 months gets applied. Used by the frontend to render the
    # "🎉 Unlocked" state and as a guard so we never double-grant.
    reward_granted_at   = models.DateTimeField(null=True, blank=True)

    created_at          = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.code} → {self.user_id}"


class ReferralAttribution(models.Model):
    """One row per referred signup. The referrer-side counter
    (ReferralCode.total_signups / qualified_signups) is the
    denormalisation; this table is the audit log."""

    referrer       = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="referrals_sent",
    )
    referred_user  = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="referral_attribution",
    )
    # Snapshot of the code at signup time so a renamed code (rare)
    # doesn't break the audit trail.
    code_snapshot  = models.CharField(max_length=20, blank=True, default="")

    # Step 1 — they signed up via the link.
    signed_up_at   = models.DateTimeField(auto_now_add=True, db_index=True)

    # Step 2 — they paid. The referral only "counts" toward the
    # reward once this is set. ``qualified_event`` records WHY we
    # marked them qualified ("subscription_paid" etc).
    qualified_at      = models.DateTimeField(null=True, blank=True, db_index=True)
    qualified_event   = models.CharField(max_length=40, blank=True, default="")
    qualified_plan    = models.CharField(max_length=40, blank=True, default="")

    class Meta:
        ordering = ["-signed_up_at"]
        indexes = [
            models.Index(fields=["referrer", "-signed_up_at"]),
            models.Index(fields=["referrer", "qualified_at"]),
        ]

    def __str__(self):
        return f"{self.referrer_id} → {self.referred_user_id} ({'✓' if self.qualified_at else '…'})"

from django.db import models

class Store(models.Model):
    PLATFORM_CHOICES = (
        ('shopify', 'Shopify'),
        ('woocommerce', 'WooCommerce'),
    )

    user = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=255)
    platform = models.CharField(max_length=50, choices=PLATFORM_CHOICES)
    store_url = models.URLField()
    api_key = models.CharField(max_length=500, blank=True, null=True)
    api_secret = models.CharField(max_length=500, blank=True, null=True)
    access_token = models.CharField(max_length=500, blank=True, null=True)
    is_active = models.BooleanField(default=True)

    # ── Shopify "expiring offline" token support ───────────────────────────
    # Shopify-2025 requires all newly-issued offline tokens to expire
    # (~24h for access tokens, ~60d for refresh tokens). Without these
    # fields, the access_token silently expires and every API call starts
    # returning HTTP 401 — the merchant has to manually click Reconnect.
    # With refresh_token persisted, we can transparently exchange it for
    # a fresh access_token before/during the next API call.
    refresh_token = models.CharField(max_length=500, blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    refresh_token_expires_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name
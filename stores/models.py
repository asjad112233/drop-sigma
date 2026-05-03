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

    def __str__(self):
        return self.name
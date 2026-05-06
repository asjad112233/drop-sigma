from django.db import models
from django.contrib.auth.models import User
from stores.models import Store
from orders.models import Order


class StockProduct(models.Model):
    store        = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="stock_products")
    product_id   = models.CharField(max_length=200)
    product_name = models.CharField(max_length=500)
    is_active    = models.BooleanField(default=True)
    synced_at    = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("store", "product_id")
        ordering = ["product_name"]

    def __str__(self):
        return f"{self.product_name} ({self.store.name})"


class StockVariant(models.Model):
    product = models.ForeignKey(StockProduct, on_delete=models.CASCADE, related_name="variants")
    color   = models.CharField(max_length=100, blank=True, default="")
    size    = models.CharField(max_length=100, blank=True, default="")
    sku     = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        unique_together = ("product", "color", "size")
        ordering = ["color", "size"]

    def __str__(self):
        parts = [self.product.product_name]
        if self.color:
            parts.append(self.color)
        if self.size:
            parts.append(self.size)
        return " / ".join(parts)


class StockEntry(models.Model):
    variant      = models.OneToOneField(StockVariant, on_delete=models.CASCADE, related_name="entry")
    quantity     = models.IntegerField(default=0)
    reserved     = models.IntegerField(default=0)
    updated_by   = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    last_updated = models.DateTimeField(auto_now=True)

    def available(self):
        return max(0, self.quantity - self.reserved)

    def __str__(self):
        return f"{self.variant} — qty:{self.quantity} res:{self.reserved}"


class StockAuditLog(models.Model):
    ACTION_CHOICES = [
        ("add",     "Stock Added"),
        ("deduct",  "Deducted (Order)"),
        ("reserve", "Reserved"),
        ("restore", "Restored"),
        ("adjust",  "Manual Adjustment"),
        ("sync",    "Product Synced"),
    ]

    variant    = models.ForeignKey(StockVariant, on_delete=models.CASCADE, related_name="audit_logs")
    order      = models.ForeignKey(Order, null=True, blank=True, on_delete=models.SET_NULL)
    action     = models.CharField(max_length=20, choices=ACTION_CHOICES)
    qty_before = models.IntegerField(default=0)
    qty_after  = models.IntegerField(default=0)
    actor      = models.CharField(max_length=200, default="Admin")
    note       = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} — {self.variant} [{self.qty_before}→{self.qty_after}]"

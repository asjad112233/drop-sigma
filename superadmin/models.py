from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import datetime
import uuid

class UserProfile(models.Model):
    user    = models.OneToOneField(User, on_delete=models.CASCADE, related_name="user_profile")
    address = models.TextField(blank=True, default="")

    def __str__(self):
        return f"Profile({self.user.username})"


PLAN_CHOICES = [
    ("trial",      "Free Trial"),
    ("basic",      "Basic"),
    ("pro",        "Pro"),
    ("enterprise", "Enterprise"),
]

STATUS_CHOICES = [
    ("active",    "Active"),
    ("trial",     "Trial"),
    ("suspended", "Suspended"),
    ("deleted",   "Deleted"),
]

PAYMENT_STATUS_CHOICES = [
    ("paid",    "Paid"),
    ("failed",  "Failed"),
    ("pending", "Pending"),
]

PLAN_PRICES = {"trial": 0, "basic": 49, "pro": 99, "enterprise": 299}


class Tenant(models.Model):
    user       = models.OneToOneField(User, on_delete=models.CASCADE, related_name="tenant_profile")
    name       = models.CharField(max_length=255)
    plan       = models.CharField(max_length=20, choices=PLAN_CHOICES, default="trial")
    status     = models.CharField(max_length=20, choices=STATUS_CHOICES, default="trial")
    notes      = models.TextField(blank=True, default="")
    flagged     = models.BooleanField(default=False)
    is_deleted  = models.BooleanField(default=False)
    deleted_at  = models.DateTimeField(null=True, blank=True)
    trial_ends  = models.DateField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    @property
    def mrr(self):
        return PLAN_PRICES.get(self.plan, 0) if self.status == "active" else 0

    @property
    def ltv(self):
        months = max(1, (timezone.now().date() - self.created_at.date()).days // 30)
        return self.mrr * months

    def __str__(self):
        return f"{self.name} ({self.plan})"


class Subscription(models.Model):
    tenant         = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="subscription")
    plan           = models.CharField(max_length=20, choices=PLAN_CHOICES, default="trial")
    price          = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    start_date     = models.DateField(default=datetime.date.today)
    renews_on      = models.DateField(null=True, blank=True)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default="paid")
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.tenant} — {self.plan}"


class TenantActivity(models.Model):
    ACTION_TYPES = [
        ("store",   "Store"),
        ("order",   "Order"),
        ("vendor",  "Vendor"),
        ("email",   "Email"),
        ("plan",    "Plan Change"),
        ("signup",  "Signup"),
        ("payment", "Payment"),
        ("general", "General"),
    ]

    tenant      = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="activities")
    action      = models.CharField(max_length=500)
    action_type = models.CharField(max_length=20, choices=ACTION_TYPES, default="general")
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.tenant.name} — {self.action}"


class Coupon(models.Model):
    DISCOUNT_TYPES = [
        ("flat",    "Flat ($)"),
        ("percent", "Percent (%)"),
    ]
    code           = models.CharField(max_length=50, unique=True)
    discount_type  = models.CharField(max_length=10, choices=DISCOUNT_TYPES, default="flat")
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    max_uses       = models.PositiveIntegerField(null=True, blank=True)
    uses           = models.PositiveIntegerField(default=0)
    is_active      = models.BooleanField(default=True)
    expires_at     = models.DateField(null=True, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        import datetime
        if not self.is_active:
            return False, "Coupon is inactive."
        if self.max_uses and self.uses >= self.max_uses:
            return False, "Coupon usage limit reached."
        if self.expires_at and datetime.date.today() > self.expires_at:
            return False, "Coupon has expired."
        return True, "ok"

    def apply(self, price):
        if self.discount_type == "flat":
            return max(0, float(price) - float(self.discount_value))
        else:
            return max(0, float(price) * (1 - float(self.discount_value) / 100))

    def __str__(self):
        return f"{self.code} ({self.discount_type}: {self.discount_value})"


class UserIPLog(models.Model):
    user         = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ip_logs')
    ip_address   = models.GenericIPAddressField()
    country      = models.CharField(max_length=100, blank=True)
    country_code = models.CharField(max_length=10,  blank=True)
    city         = models.CharField(max_length=100, blank=True)
    region       = models.CharField(max_length=100, blank=True)
    isp          = models.CharField(max_length=200, blank=True)
    lat          = models.FloatField(null=True, blank=True)
    lng          = models.FloatField(null=True, blank=True)
    browser      = models.CharField(max_length=100, blank=True)
    os_name      = models.CharField(max_length=100, blank=True)
    device_type  = models.CharField(max_length=50,  blank=True, default='desktop')
    last_seen    = models.DateTimeField(auto_now=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-last_seen']

    def __str__(self):
        return f"{self.user.username} @ {self.ip_address} ({self.city})"


class EmailVerificationToken(models.Model):
    user       = models.OneToOneField(User, on_delete=models.CASCADE, related_name="email_verification")
    token      = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used    = models.BooleanField(default=False)

    def is_expired(self):
        return timezone.now() > self.created_at + datetime.timedelta(hours=24)

    def __str__(self):
        return f"Token for {self.user.email}"

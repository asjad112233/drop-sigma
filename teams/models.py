from django.db import models


class TeamMember(models.Model):
    ROLE_CHOICES = (
        ("support", "Support"),
        ("order_manager", "Order Manager"),
        ("refund_manager", "Refund Manager"),
        ("vendor_manager", "Vendor Manager"),
        ("email_manager", "Email Manager"),
    )

    STATUS_CHOICES = (
        ("available", "Available"),
        ("busy", "Busy"),
        ("limited", "Limited"),
        ("offline", "Offline"),
    )

    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, null=True)
    role = models.CharField(max_length=50, choices=ROLE_CHOICES)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="available")
    workload = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class AssignmentRule(models.Model):
    RULE_TYPE_CHOICES = (
        ("tracking_missing", "Tracking Missing"),
        ("refund_dispute", "Refund / Dispute"),
        ("order_edit", "Order Edit"),
        ("new_email", "New Email"),
        ("overload", "Overload Above 90%"),
    )

    rule_type = models.CharField(max_length=50, choices=RULE_TYPE_CHOICES)
    assign_to_role = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.rule_type} → {self.assign_to_role}"
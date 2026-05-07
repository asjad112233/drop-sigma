from django.contrib import admin
from .models import Tenant, Subscription, TenantActivity

admin.site.register(Tenant)
admin.site.register(Subscription)
admin.site.register(TenantActivity)

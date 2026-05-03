from django.contrib import admin
from .models import Store, Order, Email

admin.site.register(Store)
admin.site.register(Order)
admin.site.register(Email)
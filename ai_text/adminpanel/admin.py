from django.contrib import admin
from .models import Subscription


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("name", "billing_type", "price", "status", "created_at")
    list_filter = ("status", "billing_type")


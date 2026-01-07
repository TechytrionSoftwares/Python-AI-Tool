from django.conf import settings
from django.db import models
from django.contrib.auth.models import User
from adminpanel.models import Subscription

class SpeechReport(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    transcript = models.TextField()
    pdf_url = models.URLField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.created_at.strftime('%Y-%m-%d')}"
        
class Recording(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recordings")
    title = models.CharField(max_length=255)

    audio_url = models.URLField(blank=True, null=True)
    pdf_url = models.URLField(blank=True, null=True)
    transcript = models.TextField(blank=True, null=True)

    filler_data = models.JSONField(default=dict, blank=True)
    pacing_data = models.JSONField(default=dict, blank=True)
    grammar_data = models.JSONField(default=dict, blank=True)
    pacing_segments = models.JSONField(default=dict, blank=True)

    hedging_data = models.JSONField(default=dict, blank=True)
    conciseness_data = models.JSONField(default=dict, blank=True)
    summary_data = models.JSONField(default=dict, blank=True)

    duration = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    STATUS_CHOICES = (
        ("uploading", "Uploading"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="uploading")
    progress = models.PositiveIntegerField(default=0)

    FILE_TYPE_CHOICES = (
        ("audio", "Audio"),
        ("video", "Video"),
    )
    file_type = models.CharField(max_length=10, choices=FILE_TYPE_CHOICES, default="audio")

    def __str__(self):
        return f"{self.title} ({self.user.username})"

class UserSubscription(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT
    )
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    cancel_at_period_end = models.BooleanField(default=False)
    customer_profile_id = models.CharField(max_length=64, null=True, blank=True)
    customer_payment_profile_id = models.CharField(max_length=64, null=True, blank=True)
    authorize_subscription_id = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True
    )
    pending_subscription = models.ForeignKey(
        Subscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pending_upgrades',
        help_text='Subscription that will take effect at next billing cycle'
    )
    pending_authorize_subscription_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text='Authorize.Net subscription ID for pending downgrade'
    )

    def __str__(self):
        return f"{self.user.username} → {self.subscription.name}"        

class Payment(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT
    )

    amount = models.DecimalField(max_digits=8, decimal_places=2)
    transaction_id = models.CharField(max_length=100)
    status = models.CharField(max_length=20)  # success / failed
    response_code = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.transaction_id} - {self.status}"        
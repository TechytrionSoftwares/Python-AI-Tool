from celery import shared_task
from django.utils import timezone
from dashboard.models import UserSubscription

@shared_task
def deactivate_expired_subscriptions():
    now = timezone.now()

    expired = UserSubscription.objects.filter(
        active=True,
        cancel_at_period_end=True,
        expires_at__lte=now
    )

    count = expired.count()

    expired.update(active=False)

    return f"Deactivated {count} expired subscriptions"

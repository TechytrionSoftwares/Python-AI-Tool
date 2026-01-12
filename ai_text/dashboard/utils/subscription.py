from django.utils import timezone
from dashboard.models import UserSubscription


def has_active_access(user):
    """
    Returns True if user has an active, non-expired subscription.
    Automatically deactivates expired subscriptions.
    """
    sub = UserSubscription.objects.filter(user=user, active=True).first()
    if not sub:
        return False

    if sub.expires_at and sub.expires_at < timezone.now():
        sub.active = False
        sub.save(update_fields=["active"])
        return False

    return True

from django.utils import timezone
from dashboard.models import UserSubscription

class CheckSubscriptionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated:
            return self.get_response(request)

        subscription = UserSubscription.objects.filter(
            user=request.user,
            active=True,
            cancel_at_period_end=True,
            expires_at__isnull=False
        ).first()

        # Deactivate ONLY if clearly expired
        if subscription and subscription.expires_at < timezone.now():
            subscription.active = False
            subscription.save(update_fields=["active"])

        return self.get_response(request)

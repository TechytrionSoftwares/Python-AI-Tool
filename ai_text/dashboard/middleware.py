from django.shortcuts import redirect
from django.contrib import messages
from django.utils import timezone
from dashboard.models import UserSubscription
from dashboard.utils.roles import is_admin

class SubscriptionRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Allow unauthenticated users
        if not request.user.is_authenticated:
            return self.get_response(request)

        # Allow admins
        if is_admin(request.user):
            return self.get_response(request)

        EXEMPT_PATHS = [
            "/dashboard/login/",
            "/dashboard/logout/",
            "/dashboard/register/",
            "/dashboard/settings/",
            "/dashboard/webhooks/",
        ]

        if any(request.path.startswith(p) for p in EXEMPT_PATHS):
            return self.get_response(request)

        subscription = UserSubscription.objects.filter(
            user=request.user
        ).first()

        #  No subscription at all
        if not subscription:
            messages.error(
                request,
                "You don’t have an active subscription."
            )
            return redirect("settings")

        #  Expired subscription
        if (
            subscription.cancel_at_period_end
            and subscription.expires_at
            and subscription.expires_at <= timezone.now()
        ):
            subscription.active = False
            subscription.save(update_fields=["active"])

            messages.error(
                request,
                "Your subscription has expired. Please renew."
            )
            return redirect("settings")

        #  Fully inactive (not cancelled, just inactive)
        if not subscription.active:
            messages.error(
                request,
                "Your subscription is inactive. Please renew."
            )
            return redirect("settings")

        #  Access allowed
        return self.get_response(request)

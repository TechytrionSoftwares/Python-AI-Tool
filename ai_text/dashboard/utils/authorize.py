import json
import logging
import requests
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
from django.utils import timezone
from dateutil.relativedelta import relativedelta

from dashboard.models import UserSubscription, Payment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def _extend_expires_at(user_sub):
    """
    Extend billing cycle WITHOUT resetting it.
    - Keeps proration correct
    - Keeps next billing date stable
    """
    base = user_sub.expires_at or timezone.now()

    if user_sub.subscription.billing_type == "monthly":
        return base + relativedelta(months=1)

    return base + relativedelta(years=1)


# ---------------------------------------------------------
# Webhook
# ---------------------------------------------------------

@csrf_exempt
def authorize_net_webhook(request):
    """
    Handles all Authorize.Net webhooks safely.
    """

    # Validation ping
    if request.method in ("GET", "HEAD"):
        return HttpResponse("OK", status=200)

    if request.method != "POST":
        return HttpResponse(status=405)

    # Parse payload safely
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
        logger.info("Authorize.Net webhook received: %s", payload)
    except Exception as e:
        logger.error("Webhook JSON parse error: %s", e)
        return HttpResponse(status=200)

    event_type = payload.get("eventType")
    data = payload.get("payload", {})

    # =====================================================
    # PAYMENT SUCCESS (INITIAL / RENEWAL / PRORATION)
    # =====================================================
    if event_type == "net.authorize.payment.authcapture.created":

        subscription_id = data.get("subscription", {}).get("id")
        transaction_id = data.get("id")
        amount = data.get("amount", 0)

        if not subscription_id or not transaction_id:
            return HttpResponse(status=200)

        try:
            user_sub = UserSubscription.objects.get(
                authorize_subscription_id=subscription_id
            )
        except UserSubscription.DoesNotExist:
            logger.warning("Subscription not found: %s", subscription_id)
            return HttpResponse(status=200)

        # -------------------------------------------------
        # Record payment (idempotent)
        # -------------------------------------------------
        Payment.objects.get_or_create(
            transaction_id=transaction_id,
            defaults={
                "user": user_sub.user,
                "subscription": user_sub.subscription,
                "amount": amount,
                "status": "success",
                "response_code": "OK",
            }
        )

        # -------------------------------------------------
        # IMPORTANT RULE:
        # - NEVER reset started_at
        # - NEVER set expires_at = now + 1 month
        # -------------------------------------------------
        user_sub.expires_at = _extend_expires_at(user_sub)
        user_sub.active = True
        user_sub.cancel_at_period_end = False

        # -------------------------------------------------
        # Apply pending downgrade AFTER successful renewal
        # -------------------------------------------------
        if (
            user_sub.pending_subscription
            and user_sub.pending_authorize_subscription_id
        ):
            logger.info(
                "Applying scheduled downgrade to %s",
                user_sub.pending_subscription.name,
            )

            try:
                cancel_payload = {
                    "ARBCancelSubscriptionRequest": {
                        "merchantAuthentication": {
                            "name": settings.AUTHORIZE_NET_LOGIN_ID,
                            "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
                        },
                        "subscriptionId": user_sub.authorize_subscription_id,
                    }
                }

                requests.post(
                    settings.AUTHORIZE_NET_ENDPOINT,
                    json=cancel_payload,
                    timeout=30,
                )

                user_sub.subscription = user_sub.pending_subscription
                user_sub.authorize_subscription_id = (
                    user_sub.pending_authorize_subscription_id
                )
                user_sub.pending_subscription = None
                user_sub.pending_authorize_subscription_id = None

            except Exception as e:
                logger.error("Failed to apply downgrade: %s", e)

        user_sub.save(
            update_fields=[
                "expires_at",
                "active",
                "cancel_at_period_end",
                "subscription",
                "authorize_subscription_id",
                "pending_subscription",
                "pending_authorize_subscription_id",
            ]
        )

        return HttpResponse(status=200)

    # =====================================================
    # PAYMENT FAILED
    # =====================================================
    elif event_type == "net.authorize.customer.subscription.failed":

        subscription_id = data.get("id")

        try:
            user_sub = UserSubscription.objects.get(
                authorize_subscription_id=subscription_id
            )
        except UserSubscription.DoesNotExist:
            return HttpResponse(status=200)

        user_sub.active = False
        user_sub.save(update_fields=["active"])

        Payment.objects.create(
            user=user_sub.user,
            subscription=user_sub.subscription,
            amount=0,
            status="failed",
            transaction_id=subscription_id or "FAILED",
            response_code="FAILED",
        )

        return HttpResponse(status=200)

    # =====================================================
    # SUBSCRIPTION CANCELLED (ACCESS CONTINUES)
    # =====================================================
    elif event_type == "net.authorize.customer.subscription.cancelled":

        subscription_id = data.get("id")

        try:
            user_sub = UserSubscription.objects.get(
                authorize_subscription_id=subscription_id
            )
            user_sub.cancel_at_period_end = True
            user_sub.save(update_fields=["cancel_at_period_end"])
        except UserSubscription.DoesNotExist:
            pass

        return HttpResponse(status=200)

    # =====================================================
    # SUBSCRIPTION CREATED / UPDATED (STORE PROFILE IDS)
    # =====================================================
    elif event_type in (
        "net.authorize.customer.subscription.created",
        "net.authorize.customer.subscription.updated",
    ):

        subscription_id = data.get("id")
        profile_data = data.get("profile", {})

        try:
            user_sub = UserSubscription.objects.get(
                authorize_subscription_id=subscription_id
            )
        except UserSubscription.DoesNotExist:
            return HttpResponse(status=200)

        updated = False

        if profile_data.get("customerProfileId"):
            user_sub.customer_profile_id = profile_data["customerProfileId"]
            updated = True

        if profile_data.get("customerPaymentProfileId"):
            user_sub.customer_payment_profile_id = profile_data[
                "customerPaymentProfileId"
            ]
            updated = True

        if updated:
            user_sub.save(
                update_fields=[
                    "customer_profile_id",
                    "customer_payment_profile_id",
                ]
            )

        return HttpResponse(status=200)

    # =====================================================
    # UNHANDLED EVENTS (SAFE IGNORE)
    # =====================================================
    logger.info("Unhandled Authorize.Net event: %s", event_type)
    return HttpResponse(status=200)

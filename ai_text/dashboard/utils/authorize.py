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


def _extend_expires_at(user_sub):
    """
    Extend the current billing cycle without resetting it.
    This keeps proration and billing dates stable.
    """
    base = user_sub.expires_at or timezone.now()

    if user_sub.subscription.billing_type == "monthly":
        return base + relativedelta(months=1)
    return base + relativedelta(years=1)


@csrf_exempt
def authorize_net_webhook(request):
    # Authorize.Net validation ping
    if request.method in ["GET", "HEAD"]:
        return HttpResponse("OK", status=200)

    if request.method != "POST":
        return HttpResponse(status=405)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
        logger.info(f"Authorize.Net webhook received: {payload}")
    except Exception as e:
        logger.error(f"Webhook payload error: {str(e)}")
        return HttpResponse(status=200)

    event_type = payload.get("eventType")
    data = payload.get("payload", {})

    # ------------------------------------------------
    # PAYMENT SUCCESS (INITIAL + AUTO-RENEWAL)
    # ------------------------------------------------
    if event_type == "net.authorize.payment.authcapture.created":
        subscription_id = data.get("subscription", {}).get("id")
        amount = data.get("amount", 0)
        transaction_id = data.get("id")

        if not subscription_id:
            return HttpResponse(status=200)

        try:
            user_sub = UserSubscription.objects.get(
                authorize_subscription_id=subscription_id
            )
        except UserSubscription.DoesNotExist:
            return HttpResponse(status=200)

        # Record payment
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


        # 🔒 EXTEND billing cycle (DO NOT RESET)
        user_sub.expires_at = _extend_expires_at(user_sub)
        user_sub.active = True
        user_sub.cancel_at_period_end = False

        # ------------------------------------------------
        # APPLY PENDING DOWNGRADE (if any)
        # ------------------------------------------------
        if user_sub.pending_subscription and user_sub.pending_authorize_subscription_id:
            logger.info(
                f"Applying pending downgrade to {user_sub.pending_subscription.name}"
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
                logger.error(f"Failed to apply downgrade: {e}")

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

    # ------------------------------------------------
    # PAYMENT FAILED
    # ------------------------------------------------
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
            transaction_id=data.get("id", "FAILED"),
            response_code="FAILED",
        )

    # ------------------------------------------------
    # SUBSCRIPTION CANCELLED (ACCESS CONTINUES)
    # ------------------------------------------------
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

    # ------------------------------------------------
    # SUBSCRIPTION CREATED / UPDATED (STORE PROFILE IDS)
    # ------------------------------------------------
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

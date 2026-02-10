import json
import logging
import requests

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db import transaction
from dateutil.relativedelta import relativedelta

from dashboard.models import UserSubscription, Payment
from dashboard.email_service import (
    send_success_payment_email,
    send_failed_payment_email,
)

logger = logging.getLogger(__name__)


# =====================================================
# Helpers
# =====================================================

def _extend_expires_at(user_sub):
    """
    Extend billing cycle safely.

    RULES:
    - NEVER reset started_at
    - NEVER set expires_at = now + 1 month blindly
    - If expired → restart from NOW
    - If active → extend from current expires_at
    """
    now = timezone.now()

    if user_sub.expires_at and user_sub.expires_at > now:
        base = user_sub.expires_at
    else:
        base = now

    if user_sub.subscription.billing_type == "monthly":
        return base + relativedelta(months=1)

    return base + relativedelta(years=1)


# =====================================================
# Authorize.Net Webhook
# =====================================================

@csrf_exempt
def authorize_net_webhook(request):

    # -------------------------------------------------
    # Validation ping (Authorize.Net requirement)
    # -------------------------------------------------
    if request.method in ("GET", "HEAD"):
        return HttpResponse("OK", status=200)

    if request.method != "POST":
        return HttpResponse(status=405)

    # -------------------------------------------------
    # Parse payload safely
    # -------------------------------------------------
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception as e:
        logger.error("Authorize.Net webhook JSON parse error: %s", e)
        return HttpResponse(status=200)

    event_type = payload.get("eventType")
    data = payload.get("payload", {})

    logger.info("Authorize.Net webhook event: %s", event_type)

    # =====================================================
    # PAYMENT SUCCESS (INITIAL / RENEWAL / PRORATION)
    # =====================================================
    if event_type == "net.authorize.payment.authcapture.created":

        subscription_id = data.get("subscription", {}).get("id")
        transaction_id = data.get("id")
        amount = data.get("amount", 0)

        if not subscription_id or not transaction_id:
            return HttpResponse(status=200)

        user_sub = UserSubscription.objects.filter(
            authorize_subscription_id=str(subscription_id)
        ).first()

        if not user_sub:
            logger.warning("Subscription not found for success payment: %s", subscription_id)
            return HttpResponse(status=200)

        # -------------------------------------------------
        # Idempotent payment log (prevents duplicates)
        # -------------------------------------------------
        payment, created = Payment.objects.get_or_create(
            transaction_id=transaction_id,
            defaults={
                "user": user_sub.user,
                "subscription": user_sub.subscription,
                "amount": amount,
                "status": "success",
                "response_code": "OK",
            },
        )

        # If webhook retried, do nothing
        if not created:
            return HttpResponse(status=200)

        # -------------------------------------------------
        # IMPORTANT RULES (DO NOT BREAK THESE)
        # -------------------------------------------------
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
                "Applying pending downgrade to %s",
                user_sub.pending_subscription.name,
            )

            try:
                # Cancel OLD Authorize.Net subscription
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

                # Switch to downgraded subscription
                user_sub.subscription = user_sub.pending_subscription
                user_sub.authorize_subscription_id = (
                    user_sub.pending_authorize_subscription_id
                )
                user_sub.pending_subscription = None
                user_sub.pending_authorize_subscription_id = None

            except Exception as e:
                logger.error("Failed to apply pending downgrade: %s", e)

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

        # -------------------------------------------------
        # Send success email AFTER DB commit
        # -------------------------------------------------
        transaction.on_commit(lambda: send_success_payment_email(
            user_sub.user,
            amount,
            transaction_id,
        ))

        return HttpResponse(status=200)

    # =====================================================
    # PAYMENT FAILED (RENEWAL DECLINED)
    # =====================================================
    elif event_type == "net.authorize.payment.authcapture.failed":

        subscription_id = data.get("subscription", {}).get("id")
        transaction_id = data.get("id")
        amount = data.get("amount", 0)

        response = data.get("response", {})
        response_reason = (
            response.get("responseReasonDescription")
            or response.get("reason")
            or "Payment failed"
        )

        if not subscription_id or not transaction_id:
            return HttpResponse(status=200)

        user_sub = UserSubscription.objects.filter(
            authorize_subscription_id=str(subscription_id)
        ).first()

        if not user_sub:
            return HttpResponse(status=200)

        # Prevent duplicate logs
        if Payment.objects.filter(transaction_id=transaction_id).exists():
            return HttpResponse(status=200)

        Payment.objects.create(
            user=user_sub.user,
            subscription=user_sub.subscription,
            amount=amount,
            transaction_id=transaction_id,
            status="failed",
            response_code="DECLINED",
            failure_reason=response_reason,
        )

        user_sub.active = False
        user_sub.save(update_fields=["active"])

        # -------------------------------------------------
        # CARD EXPIRED / PAYMENT METHOD ISSUE EMAIL
        # -------------------------------------------------
        reason_text = response_reason.lower()

        if any(keyword in reason_text for keyword in [
            "expired",
            "expiration",
            "invalid card",
            "invalid account",
            "card declined",
        ]):
            transaction.on_commit(lambda: send_card_update_required_email(
                user_sub.user,
                user_sub.subscription.name
            ))

        return HttpResponse(status=200)


    # =====================================================
    # SUBSCRIPTION CANCELLED (ACCESS UNTIL PERIOD END)
    # =====================================================
    elif event_type == "net.authorize.customer.subscription.cancelled":

        subscription_id = data.get("id")

        try:
            user_sub = UserSubscription.objects.get(
                authorize_subscription_id=str(subscription_id)
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
        profile = data.get("profile", {})

        try:
            user_sub = UserSubscription.objects.get(
                authorize_subscription_id=str(subscription_id)
            )
        except UserSubscription.DoesNotExist:
            return HttpResponse(status=200)

        updated_fields = []

        if profile.get("customerProfileId"):
            user_sub.customer_profile_id = profile["customerProfileId"]
            updated_fields.append("customer_profile_id")

        if profile.get("customerPaymentProfileId"):
            user_sub.customer_payment_profile_id = profile["customerPaymentProfileId"]
            updated_fields.append("customer_payment_profile_id")

        if updated_fields:
            user_sub.save(update_fields=updated_fields)

        return HttpResponse(status=200)

    # =====================================================
    # UNHANDLED EVENTS (SAFE IGNORE)
    # =====================================================
    logger.info("Unhandled Authorize.Net event: %s", event_type)
    return HttpResponse(status=200)

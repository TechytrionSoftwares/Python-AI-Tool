import json
import logging
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
from django.utils import timezone
from dateutil.relativedelta import relativedelta
from dashboard.models import UserSubscription, Payment

logger = logging.getLogger(__name__)


@csrf_exempt
def authorize_net_webhook(request):
    # Authorize.Net validation ping
    if request.method in ["GET", "HEAD"]:
        return HttpResponse("OK", status=200)

    if request.method != "POST":
        return HttpResponse(status=405)

    try:
        payload = json.loads(request.body.decode("utf-8"))
        logger.info(f"Webhook received: {event_type}")
    except Exception as e:
        logger.error(f"Webhook payload error: {str(e)}")
        return HttpResponse(status=400)

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
            logger.warning("Payment webhook missing subscription_id")
            return HttpResponse(status=200)

        try:
            user_sub = UserSubscription.objects.get(
                authorize_subscription_id=subscription_id
            )
        except UserSubscription.DoesNotExist:
            logger.warning(f"UserSubscription not found: {subscription_id}")
            return HttpResponse(status=200)

        # Log payment
        Payment.objects.create(
            user=user_sub.user,
            subscription=user_sub.subscription,
            amount=amount,
            status="success",
            transaction_id=transaction_id,
            response_code="OK",
        )

        # EXTEND SUBSCRIPTION PERIOD
        now = timezone.now()

        if user_sub.subscription.billing_type == "monthly":
            user_sub.started_at = now
            user_sub.expires_at = now + relativedelta(months=1)
        else:
            user_sub.started_at = now
            user_sub.expires_at = now + relativedelta(years=1)

        user_sub.active = True
        user_sub.cancel_at_period_end = False  # user paid again
        
        # CHECK FOR PENDING DOWNGRADE
        if user_sub.pending_subscription and user_sub.pending_authorize_subscription_id:
            logger.info(f"Applying pending downgrade to {user_sub.pending_subscription.name}")
            
            # Cancel OLD subscription in Authorize.Net
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
                cancel_resp = requests.post(
                    settings.AUTHORIZE_NET_ENDPOINT,
                    json=cancel_payload,
                    timeout=30,
                )
                logger.info(f"Old subscription {user_sub.authorize_subscription_id} cancelled")
                
                # Switch to the new subscription
                user_sub.subscription = user_sub.pending_subscription
                user_sub.authorize_subscription_id = user_sub.pending_authorize_subscription_id
                user_sub.pending_subscription = None
                user_sub.pending_authorize_subscription_id = None
                logger.info(f"✓ Downgrade applied successfully to {user_sub.subscription.name}")
                
            except Exception as e:
                logger.error(f"Failed to apply downgrade: {e}")
        
        user_sub.save(
            update_fields=[
                "started_at",
                "expires_at",
                "active",
                "cancel_at_period_end",
                "subscription",
                "authorize_subscription_id",
                "pending_subscription",
                "pending_authorize_subscription_id",
            ]
        )
        
        logger.info(f"Subscription renewed for user {user_sub.user.username}")

    # ------------------------------------------------
    # PAYMENT FAILED (HARD STOP)
    # ------------------------------------------------
    elif event_type == "net.authorize.customer.subscription.failed":
        subscription_id = data.get("id")

        try:
            user_sub = UserSubscription.objects.get(
                authorize_subscription_id=subscription_id
            )
        except UserSubscription.DoesNotExist:
            logger.warning(f"UserSubscription not found: {subscription_id}")
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
        
        logger.warning(f"Subscription payment failed for user {user_sub.user.username}")

    # ------------------------------------------------
    # SUBSCRIPTION CANCELLED (DO NOT KILL ACCESS)
    # ------------------------------------------------
    elif event_type == "net.authorize.customer.subscription.cancelled":
        subscription_id = data.get("id")

        try:
            user_sub = UserSubscription.objects.get(
                authorize_subscription_id=subscription_id
            )
            # Mark intent only — access stays until expires_at
            user_sub.cancel_at_period_end = True
            user_sub.save(update_fields=["cancel_at_period_end"])
            logger.info(f"Subscription cancelled for user {user_sub.user.username}")
        except UserSubscription.DoesNotExist:
            logger.warning(f"UserSubscription not found: {subscription_id}")
            pass

    # ------------------------------------------------
    # SUBSCRIPTION CREATED (CAPTURE PROFILE IDs)
    # ------------------------------------------------
    elif event_type == "net.authorize.customer.subscription.created":
        subscription_id = data.get("id")
        
        # Extract profile IDs if available
        profile_data = data.get("profile", {})
        customer_profile_id = profile_data.get("customerProfileId")
        customer_payment_profile_id = profile_data.get("customerPaymentProfileId")

        try:
            user_sub = UserSubscription.objects.get(
                authorize_subscription_id=subscription_id
            )
            
            # Store profile IDs if available
            if customer_profile_id:
                user_sub.customer_profile_id = customer_profile_id
            if customer_payment_profile_id:
                user_sub.customer_payment_profile_id = customer_payment_profile_id
            
            if customer_profile_id or customer_payment_profile_id:
                user_sub.save(update_fields=["customer_profile_id", "customer_payment_profile_id"])
                logger.info(f"Profile IDs stored for subscription {subscription_id}")
        except UserSubscription.DoesNotExist:
            logger.warning(f"UserSubscription not found: {subscription_id}")

    # ------------------------------------------------
    # SUBSCRIPTION UPDATED (UPDATE PROFILE IDs IF CHANGED)
    # ------------------------------------------------
    elif event_type == "net.authorize.customer.subscription.updated":
        subscription_id = data.get("id")
        
        profile_data = data.get("profile", {})
        customer_profile_id = profile_data.get("customerProfileId")
        customer_payment_profile_id = profile_data.get("customerPaymentProfileId")

        try:
            user_sub = UserSubscription.objects.get(
                authorize_subscription_id=subscription_id
            )
            
            updated = False
            if customer_profile_id and user_sub.customer_profile_id != customer_profile_id:
                user_sub.customer_profile_id = customer_profile_id
                updated = True
            if customer_payment_profile_id and user_sub.customer_payment_profile_id != customer_payment_profile_id:
                user_sub.customer_payment_profile_id = customer_payment_profile_id
                updated = True
            
            if updated:
                user_sub.save(update_fields=["customer_profile_id", "customer_payment_profile_id"])
                logger.info(f"Profile IDs updated for subscription {subscription_id}")
        except UserSubscription.DoesNotExist:
            logger.warning(f"UserSubscription not found: {subscription_id}")

    else:
        logger.info(f"Unhandled webhook event: {event_type}")

    return HttpResponse(status=200)
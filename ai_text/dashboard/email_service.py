from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone
from django.conf import settings
from .models import EmailLog


def send_system_email(user, to_email, subject, template_name, context):
    html_body = render_to_string(template_name, context)
    text_body = strip_tags(html_body)  

    log = EmailLog.objects.create(
        user=user,
        to_email=to_email,
        subject=subject,
        body=html_body,               
        template_name=template_name,
        status="pending"
    )

    try:
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_body,            
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to_email],
        )

        #  THIS IS THE KEY LINE
        email.attach_alternative(html_body, "text/html")

        email.send()

        log.status = "sent"
        log.sent_at = timezone.now()
        log.save(update_fields=["status", "sent_at"])

    except Exception as e:
        log.status = "failed"
        log.error_message = str(e)
        log.save(update_fields=["status", "error_message"])

def send_failed_payment_email(user, plan_name):
    subject = "Payment Failed – Action Required"

    context = {
        "user": user,
        "plan": plan_name,
        "site_name": settings.SITE_NAME,
    }

    try:
        send_system_email(
            user=user,
            to_email=user.email,
            subject=subject,
            template_name="emails/payment_failed.html",
            context=context,
        )
    except Exception as e:
        EmailLog.objects.create(
            user=user,
            to_email=user.email,
            subject=subject,
            body=str(e),
            status="failed",
            error_message=str(e),
        )

def send_success_payment_email(user, amount, transaction_id):
    subject = "Payment Successful – Thank You 🎉"

    context = {
        "user": user,
        "amount": amount,
        "transaction_id": transaction_id,
        "site_name": settings.SITE_NAME,
    }

    try:
        send_system_email(
            user=user,
            to_email=user.email,
            subject=subject,
            template_name="emails/payment_success.html",
            context=context,
        )
    except Exception as e:
        EmailLog.objects.create(
            user=user,
            to_email=user.email,
            subject=subject,
            body=str(e),
            status="failed",
            error_message=str(e),
        )        

def send_card_update_required_email(user, plan_name):
    subject = "Action Required: Update Your Payment Method"

    context = {
        "user": user,
        "plan": plan_name,
        "update_url": f"{settings.SITE_NAME}/settings/",
        "site_name": settings.SITE_NAME,
    }

    send_system_email(
        user=user,
        to_email=user.email,
        subject=subject,
        template_name="emails/update_card_required.html",
        context=context,
    )

def send_card_updated_success_email(user):
    subject = "Your Payment Method Was Updated Successfully ✅"

    context = {
        "user": user,
        "site_name": settings.SITE_NAME,
        "settings_url": f"{settings.SITE_NAME}/settings/",
    }

    try:
        send_system_email(
            user=user,
            to_email=user.email,
            subject=subject,
            template_name="emails/card_updated_success.html",
            context=context,
        )
    except Exception as e:
        # Log email failure safely
        EmailLog.objects.create(
            user=user,
            to_email=user.email,
            subject=subject,
            body=str(e),
            status="failed",
            error_message=str(e),
        )

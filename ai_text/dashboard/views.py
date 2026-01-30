from django.shortcuts import render
from django.conf import settings
import boto3, os, tempfile, time, re
from collections import Counter
from pydub import AudioSegment
import speech_recognition as sr
from moviepy.editor import VideoFileClip
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib import colors
from .models import Recording
from dateutil.relativedelta import relativedelta
from django.utils import timezone
from difflib import ndiff
from django.db import transaction
from dashboard.utils.id_encoder import decode_id, encode_id
from django.http import HttpResponseNotFound
import html
from dashboard.utils.roles import is_admin
from decimal import Decimal
from dashboard.utils.file_validators import validate_audio_video_file
from dashboard.utils.subscription import has_active_access

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from pydub.silence import detect_silence
from .utils.analyse import transcribe_audio_with_timestamps, analyze_filler_words_from_text, analyze_pacing, analyze_grammar_with_claude_sync, generate_pdf
import xmltodict

import asyncio
import json
from typing import Dict, List
from .utils.claude_utils import ask_claude_for_segments_with_timestamps
import whisper
from .utils.s3_bucket import upload_to_s3
from adminpanel.models import Subscription
from .tasks import process_recording_task

from .models import Payment, UserSubscription
from datetime import timedelta
import requests
# Add these imports to your existing code
# pip install anthropic
try:
    import anthropic
    CLAUDE_AVAILABLE = True
except ImportError:
    CLAUDE_AVAILABLE = False

SUPPORTED_AUDIO_FORMATS = ['mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac', 'wma', 'webm', 'opus']
SUPPORTED_VIDEO_FORMATS = ['mp4', 'avi', 'mov', 'mkv', 'flv', 'wmv', 'webm']
FILLER_WORDS = [
    # Basic fillers
    "um", "uh", "er", "ah", "eh", "oh", "hmm", "hm",
    
    # Conversational fillers
    "so", "well", "okay", "ok", "yeah", "yes", "yep", "nope",
    "like", "literally", "actually", "basically", "essentially",
    "fundamentally", "seriously", "honestly", "frankly",
    
    # Phrase fillers
    "i mean", "you know", "you see", "you know what i mean",
    "if you will", "as it were", "so to speak", "in a sense",
    "at the end of the day", "to be honest", "to tell you the truth",
    "believe me", "trust me", "let me tell you",
    
    # Thinking sounds
    "umm", "uhh", "err", "ahh", "mmm", "huh",
    
    # Transition fillers
    "anyway", "anyways", "anyhow", "alright", "right",
    "now", "then", "look", "listen", "see",
    
    # Emphasis fillers
    "just", "really", "very", "quite", "pretty",
    "totally", "absolutely", "definitely", "certainly",
    
    # Question tags
    "right?", "yeah?", "okay?", "see?", "no?"
]

HEDGING_WORDS = [
    # Uncertainty markers
    "maybe", "perhaps", "possibly", "probably", "presumably",
    "conceivably", "potentially", "apparently", "seemingly",
    
    # Thinking phrases
    "i think", "i believe", "i feel", "i guess", "i suppose",
    "i assume", "i imagine", "i would say", "i reckon",
    "in my opinion", "in my view", "from my perspective",
    
    # Approximations
    "kind of", "sort of", "kinda", "sorta", "type of",
    "a bit", "a little", "somewhat", "rather", "fairly",
    "relatively", "comparatively", "reasonably",
    
    # Possibility
    "might", "could", "may", "would", "should",
    "can", "could be", "might be", "may be",
    
    # Vague quantifiers
    "some", "several", "various", "certain", "a few",
    "a couple", "around", "about", "approximately",
    "roughly", "more or less", "or so",
    
    # Softeners
    "tends to", "seems to", "appears to", "looks like",
    "sounds like", "feels like", "almost", "nearly",
    "practically", "virtually", "essentially",
    
    # Qualifying phrases
    "to some extent", "to a certain degree", "in a way",
    "in some ways", "up to a point", "more or less",
    "as far as i know", "as far as i can tell",
    "if i'm not mistaken", "if memory serves",
    
    # Doubt expressions
    "i'm not sure", "i'm not certain", "i doubt",
    "it's unclear", "it's hard to say", "who knows",
    
    # Tentative language
    "arguably", "debatable", "questionable", "alleged",
    "supposed", "so-called", "purported"
]

def calculate_expires_at(started_at, billing_type):
    if billing_type == "monthly":
        return started_at + relativedelta(months=1)
    elif billing_type == "yearly":
        return started_at + relativedelta(years=1)
    return None


def checkout(request, subscription_id):
    """
    Handles Authorize.Net subscription checkout using Accept.js (ARB) with Customer Profiles
    """
    import json
    import xmltodict
    from datetime import timedelta
    from django.utils import timezone

    subscription = get_object_or_404(
        Subscription,
        id=subscription_id,
        status="published"
    )

    # -------------------------------------------------
    # GET → Show checkout page
    # -------------------------------------------------
    if request.method == "GET":
        if request.user.is_authenticated:
            existing_sub = UserSubscription.objects.filter(
                user=request.user,
                active=True
            ).first()

            if existing_sub:
                messages.warning(
                    request,
                    f"You already have an active {existing_sub.subscription.name} subscription. "
                    f"Go to Settings to manage your plan."
                )
                return redirect("settings")

        return render(
            request,
            "checkout.html",
            {
                "subscription": subscription,
                "login_id": settings.AUTHORIZE_NET_LOGIN_ID,
                "client_key": settings.AUTHORIZE_NET_CLIENT_KEY,
            }
        )

    # -------------------------------------------------
    # POST → Process payment
    # -------------------------------------------------

    # Resolve user
    if request.user.is_authenticated:
        user = request.user
        if UserSubscription.objects.filter(user=user, active=True).exists():
            messages.error(
                request,
                "You already have an active subscription. "
                "Please manage it from Settings."
            )
            return redirect("settings")
    else:
        username = request.POST.get("username")
        email = request.POST.get("email")
        password = request.POST.get("password")

        if not all([username, email, password]):
            messages.error(request, "All fields are required.")
            return redirect("checkout", subscription_id=subscription.id)

        existing_user = User.objects.filter(email=email).first()
        if existing_user and UserSubscription.objects.filter(
            user=existing_user, active=True
        ).exists():
            messages.error(
                request,
                "This email already has an active subscription. Please log in."
            )
            return redirect("login")

        request.session["pending_user"] = {
            "username": username,
            "email": email,
            "password": password,
        }
        user = None

    data_value = request.POST.get("dataValue")
    data_descriptor = request.POST.get("dataDescriptor")

    if not data_value or not data_descriptor:
        messages.error(request, "Payment token missing. Please try again.")
        return redirect("checkout", subscription_id=subscription.id)

    # User identity for Authorize.Net
    if request.user.is_authenticated:
        user_email = request.user.email
        user_name = request.user.username
    else:
        pending_user = request.session.get("pending_user")
        if not pending_user:
            messages.error(request, "Session expired. Please start again.")
            return redirect("register")
        user_email = pending_user["email"]
        user_name = pending_user["username"]

    # -------------------------------------------------
    # STEP 1: Create Customer Profile
    # -------------------------------------------------
    try:
        profile_payload = {
            "createCustomerProfileRequest": {
                "merchantAuthentication": {
                    "name": settings.AUTHORIZE_NET_LOGIN_ID,
                    "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
                },
                "profile": {
                    "merchantCustomerId": str(int(timezone.now().timestamp()))[:20],
                    "email": user_email,
                    "paymentProfiles": {
                        "customerType": "individual",
                        "billTo": {
                            "firstName": user_name,
                            "lastName": "User",
                        },
                        "payment": {
                            "opaqueData": {
                                "dataDescriptor": data_descriptor,
                                "dataValue": data_value,
                            }
                        }
                    }
                },
                "validationMode": "liveMode" if not settings.DEBUG else "testMode"
            }
        }

        profile_response = requests.post(
            settings.AUTHORIZE_NET_ENDPOINT,
            json=profile_payload,
            timeout=60,
        )

        raw_profile_text = profile_response.content.decode("utf-8-sig").strip()
        profile_data = (
            json.loads(raw_profile_text)
            if raw_profile_text.startswith("{")
            else xmltodict.parse(raw_profile_text)
        )

        profile_result = profile_data.get(
            "createCustomerProfileResponse", profile_data
        )

        if profile_result.get("messages", {}).get("resultCode") != "Ok":
            error = profile_result["messages"]["message"]
            error_text = error[0]["text"] if isinstance(error, list) else error["text"]
            messages.error(request, f"Payment setup failed: {error_text}")
            return redirect("checkout", subscription_id=subscription.id)

        customer_profile_id = profile_result.get("customerProfileId")
        payment_ids = profile_result.get("customerPaymentProfileIdList", [])
        customer_payment_profile_id = (
            payment_ids[0] if isinstance(payment_ids, list) else payment_ids
        )

        if not customer_profile_id or not customer_payment_profile_id:
            messages.error(request, "Payment profile creation failed.")
            return redirect("checkout", subscription_id=subscription.id)

    except Exception:
        messages.error(request, "Unable to create payment profile.")
        return redirect("checkout", subscription_id=subscription.id)

    # -------------------------------------------------
    # STEP 2: Create ARB Subscription
    # -------------------------------------------------
    interval_length = 1 if subscription.billing_type == "monthly" else 12

    payload = {
        "ARBCreateSubscriptionRequest": {
            "merchantAuthentication": {
                "name": settings.AUTHORIZE_NET_LOGIN_ID,
                "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
            },
            "subscription": {
                "name": f"{subscription.name} Subscription",
                "paymentSchedule": {
                    "interval": {
                        "length": interval_length,
                        "unit": "months",
                    },
                    "startDate": (timezone.now().date() + timedelta(days=1)).isoformat(),
                    "totalOccurrences": 9999,
                },
                "amount": str(subscription.price),
                "profile": {
                    "customerProfileId": customer_profile_id,
                    "customerPaymentProfileId": customer_payment_profile_id,
                },
            },
        }
    }

    response = requests.post(
        settings.AUTHORIZE_NET_ENDPOINT,
        json=payload,
        timeout=60,
    )

    parsed = json.loads(response.content.decode("utf-8-sig"))
    result = parsed.get("ARBCreateSubscriptionResponse", parsed)

    if result.get("messages", {}).get("resultCode") != "Ok":
        messages.error(request, "Subscription creation failed.")
        return redirect("checkout", subscription_id=subscription.id)

    authorize_subscription_id = result.get("subscriptionId")
    if not authorize_subscription_id:
        messages.error(request, "Subscription ID missing.")
        return redirect("checkout", subscription_id=subscription.id)

    # -------------------------------------------------
    # Create user if needed
    # -------------------------------------------------
    if not request.user.is_authenticated:
        pending_user = request.session.get("pending_user")
        user = User.objects.create_user(
            username=pending_user["username"],
            email=pending_user["email"],
            password=pending_user["password"],
        )

    # -------------------------------------------------
    # SAVE SUBSCRIPTION (CRITICAL FIX HERE)
    # -------------------------------------------------
    now = timezone.now()

    UserSubscription.objects.create(
        user=user,
        subscription=subscription,
        active=True,
        started_at=now,  # ✅ FIX
        expires_at=calculate_expires_at(now, subscription.billing_type),
        authorize_subscription_id=authorize_subscription_id,
        customer_profile_id=customer_profile_id,
        customer_payment_profile_id=customer_payment_profile_id,
    )

    Payment.objects.create(
        user=user,
        subscription=subscription,
        amount=subscription.price,
        transaction_id=authorize_subscription_id,  # kept for compatibility
        status="success",
        response_code="OK",
    )

    request.session.pop("pending_user", None)

    messages.success(request, "Subscription activated successfully!")
    return redirect("settings" if request.user.is_authenticated else "login")

# PRACTICE TAB (default dashboard)
@login_required
def practice_view(request):
    return render(request, "index3.html")

@login_required
def recording_status(request, recording_id):
    try:
        rec = Recording.objects.get(id=recording_id, user=request.user)
    except Recording.DoesNotExist:
        return JsonResponse({"deleted": True})
    return JsonResponse({
        "status": rec.status,
        "progress": rec.progress,
        "duration": rec.duration,
        "pdf_url": rec.pdf_url,
        "encoded_id": encode_id(rec.id),
    })

# RECORDING TAB (shows list)
@login_required
def recording_view(request):
    recordings = Recording.objects.filter(user=request.user).order_by('-created_at')
    paginator = Paginator(recordings, 10)  # 10 per page
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    return render(request, 'recording.html', {'recordings': page_obj})

@login_required
def change_password_ajax(request):
    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)

        if form.is_valid():
            form.save()
            return JsonResponse({"success": True})

        return JsonResponse({
            "success": False,
            "errors": form.errors
        })


# @login_required
from datetime import timedelta
from django.utils import timezone

def settings_view(request):
    active_subscription = (
        UserSubscription.objects
        .filter(user=request.user, active=True)
        .select_related("subscription")
        .first()
    )

    next_billing_date = None

    if active_subscription and active_subscription.started_at:
        billing_type = active_subscription.subscription.billing_type

        if billing_type == "monthly":
            next_billing_date = active_subscription.started_at + relativedelta(months=1)
        elif billing_type == "yearly":
            next_billing_date = active_subscription.started_at + relativedelta(years=1)

        while next_billing_date and next_billing_date <= timezone.now():
            if billing_type == "monthly":
                next_billing_date += relativedelta(months=1)
            elif billing_type == "yearly":
                next_billing_date += relativedelta(years=1)

    payments = (
        Payment.objects
        .filter(user=request.user)
        .order_by("-created_at")
    )

    has_successful_payment = False
    if active_subscription:
        has_successful_payment = Payment.objects.filter(
            user=request.user,
            subscription=active_subscription.subscription,
            status="success",
        ).exists()

    available_plan = Subscription.objects.first()

    #  FETCH ALL PLANS (unchanged logic)
    all_plans = Subscription.objects.filter(status="published").order_by("price")
    if active_subscription:
        all_plans = all_plans.filter(
            billing_type=active_subscription.subscription.billing_type
        )

    #  NEW: 48-hour restriction logic (NON-BREAKING)
    can_manage_subscription = False
    hours_remaining = None

    if active_subscription and active_subscription.started_at:
        activation_time = active_subscription.started_at + timedelta(hours=48)
        if timezone.now() >= activation_time:
            can_manage_subscription = True
        else:
            hours_remaining = int(
                (activation_time - timezone.now()).total_seconds() // 3600
            )

    return render(request, "settings.html", {
        "user": request.user,
        "active_subscription": active_subscription,
        "has_successful_payment": has_successful_payment,
        "next_billing_date": next_billing_date,
        "payments": payments,
        "started_at": timezone.now(),
        "available_plan": available_plan,
        "all_plans": all_plans,

        #  NEW (safe additions)
        "can_manage_subscription": can_manage_subscription,
        "hours_remaining": hours_remaining,
    })


import logging
logger = logging.getLogger(__name__)

@login_required
@require_POST
def resume_subscription(request):
    subscription = (
        UserSubscription.objects
        .filter(
            user=request.user,
            active=True,
            cancel_at_period_end=True,
        )
        .select_related("subscription")
        .first()
    )

    if not subscription:
        return JsonResponse({"error": "No subscription to resume"}, status=400)

    # Must have payment profiles
    if not subscription.customer_profile_id or not subscription.customer_payment_profile_id:
        return JsonResponse(
            {
                "error": (
                    "We couldn't resume your subscription automatically. "
                    "Please subscribe again to continue."
                )
            },
            status=400
        )

    # Must have existing billing cycle
    if not subscription.expires_at:
        logger.error(f"Resume failed: expires_at missing for user {subscription.user_id}")
        return JsonResponse(
            {"error": "Subscription expiry date missing. Please contact support."},
            status=400
        )

    #  BILLING CONTINUES FROM ORIGINAL CYCLE END
    start_date = subscription.expires_at.date()

    # Determine Authorize.Net interval
    if subscription.subscription.billing_type == "monthly":
        interval_length = 1
        interval_unit = "months"
    elif subscription.subscription.billing_type == "yearly":
        interval_length = 12
        interval_unit = "months"
    else:
        interval_length = 1
        interval_unit = "months"

    payload = {
        "ARBCreateSubscriptionRequest": {
            "merchantAuthentication": {
                "name": settings.AUTHORIZE_NET_LOGIN_ID,
                "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
            },
            "subscription": {
                "name": f"{subscription.subscription.name} (Resumed)",
                "paymentSchedule": {
                    "interval": {
                        "length": interval_length,
                        "unit": interval_unit,
                    },
                    "startDate": start_date.isoformat(),  # 👈 CRITICAL
                    "totalOccurrences": "9999",
                },
                "amount": str(subscription.subscription.price),
                "profile": {
                    "customerProfileId": subscription.customer_profile_id,
                    "customerPaymentProfileId": subscription.customer_payment_profile_id,
                },
            },
        }
    }

    logger.info(f"Resume payload: {json.dumps(payload, indent=2)}")

    try:
        resp = requests.post(
            settings.AUTHORIZE_NET_ENDPOINT,
            json=payload,
            timeout=30,
        )
        resp.encoding = "utf-8-sig"
        data = json.loads(resp.text)

        logger.info(f"Authorize.Net RESUME response: {json.dumps(data, indent=2)}")

        result = data.get("ARBCreateSubscriptionResponse", data)
        messages = result.get("messages", {})

        if messages.get("resultCode") != "Ok":
            error_messages = messages.get("message", [])
            if isinstance(error_messages, dict):
                error_messages = [error_messages]

            error_text = "; ".join(
                f"{m.get('code', 'N/A')}: {m.get('text', 'Unknown error')}"
                for m in error_messages
            ) if error_messages else "Unknown payment gateway error"

            logger.error(f"Authorize.Net resume failed: {error_text}")
            return JsonResponse(
                {"error": f"Unable to resume subscription: {error_text}"},
                status=400
            )

        new_subscription_id = result.get("subscriptionId")

        if not new_subscription_id:
            logger.error(f"No subscriptionId returned: {json.dumps(data)}")
            return JsonResponse(
                {"error": "No subscription ID returned from payment gateway"},
                status=400
            )

        logger.info(f"✓ Created new ARB subscription: {new_subscription_id}")

    except Exception as e:
        logger.exception("Resume subscription failed")
        return JsonResponse({"error": str(e)}, status=500)

    # ------------------------------------------------
    # LOCAL DATABASE UPDATE (SAFE)
    # ------------------------------------------------

    subscription.authorize_subscription_id = new_subscription_id
    subscription.cancel_at_period_end = False

    #  DO NOT CHANGE started_at
    #  DO NOT CHANGE expires_at

    subscription.save(update_fields=[
        "authorize_subscription_id",
        "cancel_at_period_end",
    ])

    logger.info(f"✓ Subscription resumed for user {request.user.username}")

    return JsonResponse({
        "success": True,
        "message": "Your subscription has been resumed and will continue on your original billing date."
    })



@login_required
def delete_recordings(request):
    if request.method == "POST":
        selected_ids = request.POST.getlist('selected_ids')
        Recording.objects.filter(id__in=selected_ids, user=request.user).delete()
    return redirect('recording')   
    
@login_required(login_url='login')
def recording_detail(request, encoded_id):
    """Display detailed analysis of a recording"""
    try:
        rec_id = decode_id(encoded_id)
    except Exception:
        return HttpResponseNotFound("Invalid recording ID")
    rec = get_object_or_404(Recording, pk=rec_id, user=request.user)

    # -------------------------
    # Init
    # -------------------------
    grammar_inline = ""
    grammar_data = {}
    grammar_results = []
    conciseness = 0
    speaking_tips = []

    conciseness_data = rec.conciseness_data or {}

    # -------------------------
    # Conciseness score
    # -------------------------
    if rec.filler_data and rec.pacing_data:
        fillers_per_min = rec.filler_data.get("fillers_per_minute", 0)
        conciseness = max(0, min(100, 100 - (fillers_per_min * 10)))

    # -------------------------
    #  Grammar (RUN ONCE + BACKWARD SAFE)
    # -------------------------
    if rec.transcript and not rec.grammar_data:
        # First-time grammar generation
        grammar_data = analyze_grammar_with_claude_sync(rec.transcript)

        # Store FULL structured payload
        rec.grammar_data = grammar_data
        rec.save(update_fields=["grammar_data"])
    else:
        grammar_data = rec.grammar_data or {}

    #  Backward compatibility (old records stored as LIST)
    if isinstance(grammar_data, list):
        grammar_results = grammar_data
        grammar_inline = ""
        grammar_data = {
            "analysis": grammar_results,
            "inline": ""
        }
    else:
        grammar_results = grammar_data.get("analysis", [])
        grammar_inline = grammar_data.get("inline", "")

    # -------------------------
    # Speaking tips
    # -------------------------
    speaking_tips = generate_speaking_tips(
        rec.filler_data,
        rec.pacing_data,
        grammar_data
    )

    filler_frequency = {}
    fillers_per_minute = 0

    if rec.filler_data:
        filler_frequency = rec.filler_data.get("filler_frequency", {})
        fillers_per_minute = rec.filler_data.get("fillers_per_minute", 0)
        conciseness = max(0, min(100, 100 - (fillers_per_minute * 10)))

    # -------------------------
    # Segments & pacing
    # -------------------------
    segments = []
    pace_segments = []

    if isinstance(rec.pacing_segments, dict):
        segments = rec.pacing_segments.get("segments", [])
        pace_segments = rec.pacing_segments.get("pace_segments", [])

    pacing_analysis = analyze_pacing(rec.transcript, rec.duration)

    talk_time_data = calculate_talk_time_percentage(
        segments=segments,
        total_duration=rec.duration
    )

    # -------------------------
    # Summary (run once)
    # -------------------------
    summary_data = rec.summary_data

    if rec.transcript and not summary_data:
        try:
            summary_data = generate_transcript_summary_with_claude(rec.transcript)
            rec.summary_data = summary_data
            rec.save(update_fields=["summary_data"])
        except Exception as e:
            print("Summary generation failed:", e)
            summary_data = {"summary": [], "actions": []}

    # -------------------------
    # Context
    # -------------------------
    context = {
        "rec": rec,
        "segments": segments,
        "pace_segments": pace_segments,

        "grammar_inline": grammar_inline,
        "grammar_data": grammar_data,
        "grammar_results": grammar_results,

        "conciseness": conciseness,
        "speaking_tips": speaking_tips,

        "talk_time": talk_time_data,
        "filler_frequency": filler_frequency,
        "fillers_per_minute": fillers_per_minute,
        "conciseness_data": conciseness_data,
        "pacing_analysis": pacing_analysis,

        "summary_data": summary_data,
    }

    return render(request, "recording_detail.html", context)


def generate_transcript_summary_with_claude(transcript: str) -> dict:
    """
    Generate a meeting-style summary and action items from transcript
    """
    if not transcript.strip():
        return {"summary": [], "actions": []}

    if not CLAUDE_AVAILABLE:
        return {"summary": [], "actions": []}

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    prompt = f"""
    You are summarizing a business meeting transcript.

    OUTPUT FORMAT (STRICT JSON):
    {{
      "summary": [
        "Bullet point 1",
        "Bullet point 2"
      ],
      "actions": [
        "Action item 1",
        "Action item 2"
      ]
    }}

    RULES:
    - Use clear, concise bullet points
    - No filler words
    - Past tense for summary
    - Imperative tense for action items
    - 6–10 summary bullets max
    - 3–6 action items max

    TRANSCRIPT:
    \"\"\"{transcript}\"\"\"
    """

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )

    text = message.content[0].text.strip()

    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()

    return json.loads(text)

def register_user(request):
    #  Always available (GET + POST)
    subscriptions = Subscription.objects.filter(
        status="published"
    ).order_by("price")

    #  GET request → just show page
    if request.method == "GET":
        return render(
            request,
            "register.html",
            {"subscriptions": subscriptions}
        )

    #  POST request → now we read form data
    username = request.POST.get("username")
    email = request.POST.get("email")
    password = request.POST.get("password")
    subscription_id = request.POST.get("subscription_id")

    if not all([username, email, password, subscription_id]):
        messages.error(request, "All fields are required")
        return redirect("register")

    #  DO NOT create user here anymore
    # Save data for payment step
    request.session["pending_user"] = {
        "username": username,
        "email": email,
        "password": password,
        "subscription_id": subscription_id,
    }

    return redirect("checkout", subscription_id=subscription_id)


def highlight_repeated_phrases(text, repetitions):
    for r in repetitions:
        phrase = re.escape(r["phrase"])
        text = re.sub(
            rf"\b({phrase})\b",
            r'<span class="concise-repeat">\1</span>',
            text,
            flags=re.IGNORECASE
        )
    return text

def login_user(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")

        user = authenticate(request, username=username, password=password)

        if not user:
            messages.error(request, "Invalid credentials")
            return redirect("login")

        #  Log the user in FIRST
        login(request, user)

        #  Admins bypass subscription checks
        if is_admin(user):
            return redirect("admin-dashboard")

        #  Fetch subscription (active OR cancel-at-period-end)
        subscription = (
            UserSubscription.objects
            .filter(user=user)
            .first()
        )

        #  No subscription at all
        if not subscription:
            messages.warning(
                request,
                "You don’t have an active subscription. Please choose a plan."
            )
            return redirect("settings")

        #  Subscription expired
        if subscription.expires_at and subscription.expires_at < timezone.now():
            subscription.active = False
            subscription.save(update_fields=["active"])

            messages.warning(
                request,
                "Your subscription has expired. Please renew."
            )
            return redirect("settings")

        #  Subscription inactive
        if not subscription.active:
            messages.warning(
                request,
                "Your subscription is inactive. Please renew."
            )
            return redirect("settings")

        #  All good → dashboard
        return redirect("practice")

    return render(request, "login.html")


@login_required(login_url='login')
def dashboard_home(request):
    return render(request, 'index3.html')

def logout_user(request):
    logout(request)
    return redirect("login")

@login_required
def dashboard_redirect(request):
    return redirect('practice')

def analyze_filler_words(transcript: str, duration_sec: float):
    if not transcript or duration_sec <= 0:
        return None

    text = transcript.lower()

    counts = Counter()
    total_words = len(text.split())

    for fw in FILLER_WORDS:
        # word boundary, handles "i mean"
        pattern = r'\b' + re.escape(fw) + r'\b'
        matches = re.findall(pattern, text)
        if matches:
            counts[fw] += len(matches)

    total_fillers = sum(counts.values())
    minutes = duration_sec / 60

    return {
        "total_fillers": total_fillers,
        "fillers_per_minute": round(total_fillers / minutes, 2) if minutes else 0,
        "filler_frequency": dict(counts),
        "feedback": (
            "No significant filler words detected."
            if total_fillers < 3
            else "You rely on filler words frequently. Try pausing silently instead."
        )
    }

def analyze_fillers_from_pauses(audio_path, audio_duration):
    """
    Detect filler-like hesitation based on silence duration.
    A pause between 300ms–1500ms is treated as a filler hesitation.
    """

    sound = AudioSegment.from_file(audio_path)

    # Detect silences (in milliseconds)
    silences = detect_silence(
        sound,
        min_silence_len=300,   # 0.3s = hesitation start
        silence_thresh=sound.dBFS - 16
    )

    filler_pauses = []
    for start_ms, end_ms in silences:
        duration_ms = end_ms - start_ms

        # Ignore long natural pauses (> 1.5s)
        if 300 <= duration_ms <= 1500:
            filler_pauses.append(duration_ms)

    total_fillers = len(filler_pauses)
    minutes = audio_duration / 60 if audio_duration else 1

    return {
        "type": "pause_based",
        "total_fillers": total_fillers,
        "fillers_per_minute": round(total_fillers / minutes, 2),
        "pause_durations_ms": filler_pauses,
        "feedback": (
            "Good pacing with natural pauses"
            if total_fillers < 5
            else "Frequent hesitation pauses detected"
        )
    }

def generate_speaking_tips(filler_data, pacing_data, grammar_data):
    """
    Generate personalized speaking improvement tips based on analysis
    """
    tips = []
    
    # Filler word tips
    if filler_data and filler_data.get('fillers_per_minute', 0) > 3:
        top_fillers = sorted(
            filler_data.get('filler_frequency', {}).items(),
            key=lambda x: x[1],
            reverse=True
        )[:3]
        
        if top_fillers:
            filler_names = ", ".join([f"'{word}'" for word, _ in top_fillers])
            tips.append({
                'category': 'Filler Words',
                'issue': f'You frequently use: {filler_names}',
                'suggestion': 'Try pausing briefly instead of using filler words. Practice speaking more slowly and take a breath before continuing.',
                'severity': 'medium' if filler_data.get('fillers_per_minute', 0) < 5 else 'high'
            })
    
    # Pacing tips
    if pacing_data:
        wpm = pacing_data.get('wpm', 0)
        if wpm < 125:
            tips.append({
                'category': 'Speaking Pace',
                'issue': f'Speaking slowly at {wpm} words per minute',
                'suggestion': 'Try to increase your pace slightly. Practice reading aloud and gradually speed up while maintaining clarity.',
                'severity': 'low'
            })
        elif wpm > 160:
            tips.append({
                'category': 'Speaking Pace',
                'issue': f'Speaking quickly at {wpm} words per minute',
                'suggestion': 'Slow down to improve clarity. Take deliberate pauses between sentences to give listeners time to process.',
                'severity': 'medium'
            })
    
    # Grammar tips (only for genuine spoken errors)
    if grammar_data and grammar_data.get('issues', 0) > 0:
        error_types = {}
        for error in grammar_data.get('analysis', []):
            error_type = error.get('type', 'other')
            error_types[error_type] = error_types.get(error_type, 0) + 1
        
        if error_types:
            most_common = max(error_types.items(), key=lambda x: x[1])
            error_type, count = most_common
            
            type_suggestions = {
                'article': 'Remember to use articles (a, an, the) before nouns. Practice by describing objects around you.',
                'verb': 'Focus on subject-verb agreement. Practice conjugating common verbs in different tenses.',
                'tense': 'Be consistent with your verb tenses. If you\'re telling a past story, keep all verbs in past tense.',
                'plural': 'Pay attention to singular vs plural forms. Practice counting objects aloud.',
                'pronoun': 'Review pronoun usage. Practice substituting nouns with correct pronouns.'
            }
            
            tips.append({
                'category': 'Grammar',
                'issue': f'{count} {error_type} error(s) detected',
                'suggestion': type_suggestions.get(error_type, 'Review basic grammar rules and practice speaking in complete sentences.'),
                'severity': 'medium' if count < 3 else 'high'
            })
    
    # If no issues found
    if not tips:
        tips.append({
            'category': 'Overall',
            'issue': 'Great speaking performance!',
            'suggestion': 'Your speaking is clear and well-paced. Keep practicing to maintain this level.',
            'severity': 'none'
        })
    
    return tips

def calculate_talk_time_percentage(segments, total_duration):
    if not segments or total_duration <= 0:
        return {
            "talk_time_percent": 0,
            "speaking_seconds": 0,
            "silence_seconds": total_duration
        }

    speaking_seconds = sum(
        max(0, seg["end_s"] - seg["start_s"])
        for seg in segments
        if "start_s" in seg and "end_s" in seg
    )

    speaking_seconds = min(speaking_seconds, total_duration)
    silence_seconds = max(0, total_duration - speaking_seconds)

    talk_time_percent = round((speaking_seconds / total_duration) * 100)

    return {
        "talk_time_percent": talk_time_percent,
        "speaking_seconds": round(speaking_seconds, 2),
        "silence_seconds": round(silence_seconds, 2)
    }

def calculate_confidence_score(filler_data, pacing_data):
    score = 100

    pauses_per_min = filler_data.get("fillers_per_minute", 0)
    wpm = pacing_data.get("wpm", 0)

    score -= pauses_per_min * 8

    if wpm < 120 or wpm > 170:
        score -= 10

    return max(0, min(100, int(score)))

@login_required
def speech_tx(request):
    # GET → show page
    if request.method == "GET":
        return render(request, "index3.html")

    # POST → MULTI file AJAX upload
    if request.method == "POST":
        audio_files = request.FILES.getlist("audio_file")
        video_files = request.FILES.getlist("video_file")

        files = audio_files + video_files

        if not files:
            return JsonResponse({"error": "No files uploaded"}, status=400)

        created_recordings = []
        rejected_files = []

        for file in files:
            # VALIDATE FILE BEFORE PROCESSING
            is_valid, error_msg, file_type = validate_audio_video_file(file)
            
            if not is_valid:
                rejected_files.append({
                    'filename': file.name,
                    'error': error_msg
                })
                logger.warning(f"File rejected: {file.name} - {error_msg}")
                continue  # Skip this file
            
            # File is valid, proceed with upload
            try:
                # Create DB row
                recording = Recording.objects.create(
                    user=request.user,
                    title=file.name,
                    status="uploading",
                    file_type=file_type,
                )

                # Upload to S3
                s3_key = f"uploads/{file_type}/{recording.id}_{file.name}"
                s3_url = upload_to_s3(file, s3_key)

                if not s3_url:
                    rejected_files.append({
                        'filename': file.name,
                        'error': 'Failed to upload to storage'
                    })
                    recording.delete()  # Clean up database entry
                    continue

                # Update recording
                recording.audio_url = s3_url
                recording.status = "processing"
                recording.progress = 10
                recording.save()

                # Enqueue background job
                process_recording_task.delay(recording.id)

                created_recordings.append({
                    'id': recording.id,
                    'filename': file.name
                })
                
                logger.info(f"✓ File accepted and queued: {file.name}")
                
            except Exception as e:
                logger.error(f"Error processing {file.name}: {str(e)}")
                rejected_files.append({
                    'filename': file.name,
                    'error': str(e)
                })

        # Prepare response
        response_data = {
            "success": len(created_recordings) > 0,
            "recording_ids": [r['id'] for r in created_recordings],
            "count": len(created_recordings),
            "accepted": created_recordings,
            "rejected": rejected_files
        }
        
        if rejected_files:
            response_data["message"] = f"{len(created_recordings)} file(s) uploaded, {len(rejected_files)} rejected"
        else:
            response_data["message"] = f"{len(created_recordings)} file(s) uploaded successfully"

        # Return 200 even if some files rejected (partial success)
        return JsonResponse(response_data)

    return JsonResponse({"error": "Method not allowed"}, status=405)

@login_required
def cancel_subscription(request):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request"}, status=400)

    subscription = (
        UserSubscription.objects
        .filter(user=request.user, active=True)
        .first()
    )

    if not subscription:
        return JsonResponse({"error": "No active subscription"}, status=400)

    # Already cancelled
    if subscription.cancel_at_period_end:
        return JsonResponse({
            "error": "Subscription is already scheduled for cancellation",
            "expires_at": (
                subscription.expires_at.strftime("%B %d, %Y")
                if subscription.expires_at else None
            )
        }, status=400)

    # ------------------------------------------------
    #  Cancel at Authorize.Net (if applicable)
    # ------------------------------------------------
    if subscription.authorize_subscription_id:
        payload = {
            "ARBCancelSubscriptionRequest": {
                "merchantAuthentication": {
                    "name": settings.AUTHORIZE_NET_LOGIN_ID,
                    "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
                },
                "subscriptionId": subscription.authorize_subscription_id,
            }
        }

        try:
            response = requests.post(
                settings.AUTHORIZE_NET_ENDPOINT,
                json=payload,
                timeout=30,
            )

            if response.status_code != 200:
                return JsonResponse(
                    {"error": "Payment gateway error"},
                    status=500
                )

            raw_text = response.content.decode("utf-8-sig").strip()

            if raw_text.startswith("{"):
                data = json.loads(raw_text)
            elif raw_text.startswith("<"):
                import xmltodict
                data = xmltodict.parse(raw_text)
            else:
                return JsonResponse(
                    {"error": "Invalid gateway response"},
                    status=500
                )

            arb_response = (
                data.get("ARBCancelSubscriptionResponse")
                or data.get("response")
                or data
            )

            messages_block = arb_response.get("messages", {})
            if messages_block.get("resultCode") != "Ok":
                return JsonResponse(
                    {"error": "Unable to cancel subscription"},
                    status=400
                )

        except Exception as e:
            return JsonResponse(
                {"error": f"Gateway error: {str(e)}"},
                status=500
            )

    # ------------------------------------------------
    #  Update local subscription safely
    # ------------------------------------------------
    try:
        subscription.cancel_at_period_end = True
        subscription.active = True  #  KEEP ACCESS

        #  Correct expiry calculation (NO loops)
        if subscription.started_at:
            if subscription.subscription.billing_type == "monthly":
                subscription.expires_at = (
                    subscription.started_at + relativedelta(months=1)
                )
            else:
                subscription.expires_at = (
                    subscription.started_at + relativedelta(years=1)
                )
        else:
            subscription.expires_at = timezone.now() + (
                relativedelta(months=1)
                if subscription.subscription.billing_type == "monthly"
                else relativedelta(years=1)
            )

        # 🛡 Safety guard — never allow past expiry
        if subscription.expires_at <= timezone.now():
            subscription.expires_at = timezone.now() + relativedelta(days=1)

        subscription.save(
            update_fields=["cancel_at_period_end", "expires_at", "active"]
        )

        return JsonResponse({
            "success": True,
            "message": (
                "Your subscription will remain active until "
                f"{subscription.expires_at.strftime('%B %d, %Y')}"
            )
        })

    except Exception as e:
        return JsonResponse(
            {"error": f"Database error: {str(e)}"},
            status=500
        )

logger = logging.getLogger(__name__)


def _cancel_subscription_in_authorize(subscription_id):
    """Cancel a subscription in Authorize.Net"""
    cancel_payload = {
        "ARBCancelSubscriptionRequest": {
            "merchantAuthentication": {
                "name": settings.AUTHORIZE_NET_LOGIN_ID,
                "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
            },
            "subscriptionId": subscription_id,
        }
    }

    try:
        response = requests.post(
            settings.AUTHORIZE_NET_ENDPOINT,
            json=cancel_payload,
            timeout=30,
        )
        data = parse_authorize_response(response)
        logger.info(f"Cancelled subscription {subscription_id}: {data}")
        return True
    except Exception as e:
        logger.error(f"Failed to cancel subscription {subscription_id}: {e}")
        return False


def _cancel_pending_subscription_if_any(current_sub):
    """Cancel any pending future subscription"""
    if not current_sub.pending_authorize_subscription_id:
        return

    _cancel_subscription_in_authorize(current_sub.pending_authorize_subscription_id)

    current_sub.pending_subscription = None
    current_sub.pending_authorize_subscription_id = None
    current_sub.save(update_fields=[
        "pending_subscription",
        "pending_authorize_subscription_id"
    ])


def parse_authorize_response(response):
    """Safely parse Authorize.Net responses (handles UTF-8 BOM)"""
    return json.loads(response.content.decode("utf-8-sig"))


@login_required
@require_POST
def change_subscription_plan(request):
    try:
        data = json.loads(request.body)
        new_subscription_id = data.get("subscription_id")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request data"}, status=400)

    if not new_subscription_id:
        return JsonResponse({"error": "Subscription ID required"}, status=400)

    with transaction.atomic():
        current_sub = (
            UserSubscription.objects
            .select_for_update()
            .filter(user=request.user, active=True)
            .select_related("subscription")
            .first()
        )

        if not current_sub:
            return JsonResponse({"error": "No active subscription found"}, status=400)

        if current_sub.is_processing:
            return JsonResponse(
                {"error": "Subscription change already in progress"},
                status=409
            )

        current_sub.is_processing = True
        current_sub.save(update_fields=["is_processing"])

    try:
        new_plan = Subscription.objects.get(id=new_subscription_id, status="published")
    except Subscription.DoesNotExist:
        _release_lock(current_sub)
        return JsonResponse({"error": "Invalid subscription plan"}, status=400)

    if current_sub.subscription.id == new_plan.id:
        _release_lock(current_sub)
        return JsonResponse({"error": "You are already on this plan"}, status=400)

    if current_sub.subscription.billing_type != new_plan.billing_type:
        _release_lock(current_sub)
        return JsonResponse(
            {"error": "Cannot switch between monthly and yearly plans."},
            status=400,
        )

    try:
        if new_plan.price > current_sub.subscription.price:
            response = _safe_upgrade_subscription(request, current_sub, new_plan)
        else:
            response = _handle_downgrade(request, current_sub, new_plan)
    finally:
        _release_lock(current_sub)

    return response


def _release_lock(current_sub):
    """Release processing lock"""
    current_sub.is_processing = False
    current_sub.save(update_fields=["is_processing"])


def _calculate_prorated_amount(current_sub, new_plan):
    """Calculate prorated upgrade charge based on remaining time"""
    if not current_sub.started_at or not current_sub.expires_at:
        return Decimal("0.00")

    now = timezone.now()

    # If subscription expired, no proration
    if now >= current_sub.expires_at:
        return Decimal("0.00")

    total_seconds = (current_sub.expires_at - current_sub.started_at).total_seconds()
    remaining_seconds = (current_sub.expires_at - now).total_seconds()

    if total_seconds <= 0 or remaining_seconds <= 0:
        return Decimal("0.00")

    remaining_ratio = Decimal(remaining_seconds) / Decimal(total_seconds)

    old_price = Decimal(current_sub.subscription.price)
    new_price = Decimal(new_plan.price)

    # Charge the DIFFERENCE for remaining time
    prorated_charge = (new_price - old_price) * remaining_ratio

    # Never negative, always rounded to 2 decimals
    return max(prorated_charge.quantize(Decimal("0.01")), Decimal("0.00"))

def _cancel_authorize_subscription(subscription_id):
    payload = {
        "ARBCancelSubscriptionRequest": {
            "merchantAuthentication": {
                "name": settings.AUTHORIZE_NET_LOGIN_ID,
                "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
            },
            "subscriptionId": subscription_id
        }
    }

    resp = requests.post(
        settings.AUTHORIZE_NET_ENDPOINT,
        json=payload,
        timeout=30,
    )

    data = parse_authorize_response(resp)
    if data.get("messages", {}).get("resultCode") != "Ok":
        raise Exception(f"Failed to cancel ARB subscription {subscription_id}")



def _safe_upgrade_subscription(request, current_sub, new_plan):
    """
    STABLE UPGRADE FLOW (AUTHORIZE.NET SAFE)

    - Cancel any extra/pending ARB subscriptions
    - Charge proration immediately
    - Update ARB amount for next billing
    - Upgrade user access immediately
    - Keep ONE ARB subscription only
    """

    logger.info(f"Upgrading {current_sub.subscription.name} → {new_plan.name}")

    prorated_amount = _calculate_prorated_amount(current_sub, new_plan)
    old_price = current_sub.subscription.price
    new_price = new_plan.price

    try:
        with transaction.atomic():

            # 0. Cancel pending downgrade ARB (if exists)
            if current_sub.pending_authorize_subscription_id:
                logger.warning(
                    f"Cancelling pending ARB "
                    f"{current_sub.pending_authorize_subscription_id}"
                )
                _cancel_authorize_subscription(
                    current_sub.pending_authorize_subscription_id
                )
                current_sub.pending_authorize_subscription_id = None
                current_sub.pending_subscription = None

            # 1. Safety: ensure ONLY ONE ARB exists
            # If multiple ARBs were accidentally created earlier,
            # cancel the one that does NOT match the active subscription
            if (
                current_sub.authorize_subscription_id
                and current_sub.authorize_subscription_id
                != current_sub.pending_authorize_subscription_id
            ):
                # Nothing extra to cancel here normally,
                # but this block exists for future-proofing
                pass

            # 2. Charge proration
            if prorated_amount > 0:
                charge_payload = {
                    "createTransactionRequest": {
                        "merchantAuthentication": {
                            "name": settings.AUTHORIZE_NET_LOGIN_ID,
                            "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
                        },
                        "transactionRequest": {
                            "transactionType": "authCaptureTransaction",
                            "amount": str(prorated_amount),
                            "profile": {
                                "customerProfileId": current_sub.customer_profile_id,
                                "paymentProfile": {
                                    "paymentProfileId": current_sub.customer_payment_profile_id
                                }
                            }
                        }
                    }
                }

                charge_resp = requests.post(
                    settings.AUTHORIZE_NET_ENDPOINT,
                    json=charge_payload,
                    timeout=30,
                )

                charge_data = parse_authorize_response(charge_resp)
                if charge_data.get("transactionResponse", {}).get("responseCode") != "1":
                    raise Exception("Proration payment failed")

                Payment.objects.create(
                    user=request.user,
                    subscription=new_plan,
                    amount=prorated_amount,
                    transaction_id=charge_data["transactionResponse"]["transId"],
                    status="success",
                )

            # 3. Update ARB amount if price changed
            if old_price != new_price and current_sub.authorize_subscription_id:
                arb_payload = {
                    "ARBUpdateSubscriptionRequest": {
                        "merchantAuthentication": {
                            "name": settings.AUTHORIZE_NET_LOGIN_ID,
                            "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
                        },
                        "subscriptionId": current_sub.authorize_subscription_id,
                        "subscription": {
                            "amount": str(new_price)
                        }
                    }
                }

                arb_resp = requests.post(
                    settings.AUTHORIZE_NET_ENDPOINT,
                    json=arb_payload,
                    timeout=30,
                )

                arb_data = parse_authorize_response(arb_resp)
                if arb_data.get("messages", {}).get("resultCode") != "Ok":
                    raise Exception(f"ARB update failed: {arb_data}")

            # 4. Upgrade locally
            current_sub.subscription = new_plan
            current_sub.active = True
            current_sub.cancel_at_period_end = False
            current_sub.save(
                update_fields=[
                    "subscription",
                    "active",
                    "cancel_at_period_end",
                    "pending_subscription",
                    "pending_authorize_subscription_id",
                ]
            )

            logger.info("Upgrade completed successfully")

            return JsonResponse({
                "success": True,
                "message": (
                    f"Successfully upgraded to {new_plan.name}. "
                    f"Your billing date remains "
                    f"{current_sub.expires_at.strftime('%B %d, %Y')}."
                ),
                "prorated_charge": str(prorated_amount) if prorated_amount > 0 else None,
            })

    except Exception:
        logger.exception("Upgrade failed")
        return JsonResponse(
            {"error": "Unable to upgrade subscription. Please contact support."},
            status=500
        )





# def _handle_downgrade(request, current_sub, new_plan):
#     """
#     Handle subscription downgrades:
#     - Schedule the downgrade to take effect at next billing date
#     - User keeps current plan until then
#     """
    
#     try:
#         with transaction.atomic():
#             # Cancel any existing pending subscription
#             _cancel_pending_subscription_if_any(current_sub)

#             # Calculate next billing date
#             next_billing_date = current_sub.expires_at.date()
#             today = timezone.now().date()
            
#             # If billing date passed, calculate next occurrence
#             if next_billing_date <= today:
#                 if new_plan.billing_type == "monthly":
#                     next_month = today.replace(day=1) + timedelta(days=32)
#                     try:
#                         next_billing_date = next_month.replace(day=current_sub.started_at.day)
#                     except ValueError:
#                         next_billing_date = next_month.replace(day=28)
#                 else:  # yearly
#                     next_year = today.year + 1
#                     try:
#                         next_billing_date = today.replace(year=next_year, day=current_sub.started_at.day)
#                     except ValueError:
#                         next_billing_date = today.replace(year=next_year, day=28)
            
#             # Make sure it's in the future
#             if next_billing_date <= today:
#                 next_billing_date = today + timedelta(days=1)

#             interval_length = 1 if new_plan.billing_type == "monthly" else 12

#             # Create NEW subscription starting at next billing date
#             create_payload = {
#                 "ARBCreateSubscriptionRequest": {
#                     "merchantAuthentication": {
#                         "name": settings.AUTHORIZE_NET_LOGIN_ID,
#                         "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
#                     },
#                     "subscription": {
#                         "name": new_plan.name,
#                         "paymentSchedule": {
#                             "interval": {
#                                 "length": interval_length,
#                                 "unit": "months"
#                             },
#                             "startDate": next_billing_date.isoformat(),
#                             "totalOccurrences": "9999",
#                         },
#                         "amount": str(new_plan.price),
#                         "profile": {
#                             "customerProfileId": current_sub.customer_profile_id,
#                             "customerPaymentProfileId": current_sub.customer_payment_profile_id,
#                         },
#                     }
#                 }
#             }

#             create_resp = requests.post(
#                 settings.AUTHORIZE_NET_ENDPOINT,
#                 json=create_payload,
#                 timeout=30,
#             )

#             create_data = parse_authorize_response(create_resp)
#             response = create_data.get("ARBCreateSubscriptionResponse", {})
            
#             # Check for errors
#             result_code = response.get("messages", {}).get("resultCode")
#             if result_code != "Ok":
#                 messages = response.get("messages", {})
#                 message_list = messages.get("message", [])
#                 if message_list:
#                     error_text = message_list[0].get("text", "Unknown error")
#                 else:
#                     error_text = "Subscription creation failed"
                
#                 raise Exception(error_text)
            
#             new_arb_id = response.get("subscriptionId")
#             if not new_arb_id:
#                 raise Exception("No subscription ID returned from Authorize.Net")

#             # Store pending subscription info (DON'T cancel current yet)
#             current_sub.pending_subscription = new_plan
#             current_sub.pending_authorize_subscription_id = new_arb_id
#             current_sub.save(update_fields=[
#                 "pending_subscription",
#                 "pending_authorize_subscription_id"
#             ])

#             return JsonResponse({
#                 "success": True,
#                 "message": (
#                     f"Your plan will change to {new_plan.name} on "
#                     f"{next_billing_date.strftime('%B %d, %Y')}. "
#                     f"You'll continue to have access to {current_sub.subscription.name} until then."
#                 ),
#                 "effective_date": next_billing_date.strftime('%Y-%m-%d'),
#             })

#     except Exception as e:
#         logger.exception("Downgrade failed")
#         return JsonResponse({"error": str(e)}, status=500)



@login_required
@require_POST
def preview_plan_change(request):
    try:
        data = json.loads(request.body)
        new_subscription_id = data.get("subscription_id")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request data"}, status=400)

    if not new_subscription_id:
        return JsonResponse({"error": "Subscription ID required"}, status=400)

    current_sub = (
        UserSubscription.objects
        .filter(user=request.user, active=True)
        .select_related("subscription")
        .first()
    )

    if not current_sub:
        return JsonResponse({"error": "No active subscription found"}, status=400)

    new_plan = get_object_or_404(
        Subscription,
        id=new_subscription_id,
        status="published"
    )

    # Prevent invalid billing switches
    if new_plan.billing_type != current_sub.subscription.billing_type:
        return JsonResponse({
            "error": "Cannot switch between monthly and yearly plans."
        }, status=400)

    if current_sub.subscription.id == new_plan.id:
        return JsonResponse({"error": "You are already on this plan"}, status=400)

    is_upgrade = new_plan.price > current_sub.subscription.price

    # expires_at MUST exist
    if not current_sub.expires_at:
        return JsonResponse({
            "error": "Billing cycle information missing. Please contact support."
        }, status=400)

    if is_upgrade:
        prorated_amount = _calculate_prorated_amount(current_sub, new_plan)

        return JsonResponse({
            "success": True,
            "is_upgrade": True,
            "current_plan": current_sub.subscription.name,
            "new_plan": new_plan.name,
            "current_price": str(current_sub.subscription.price),
            "new_price": str(new_plan.price),
            "prorated_charge": str(prorated_amount),
            "next_billing_date": current_sub.expires_at.strftime("%B %d, %Y"),
        })

    # Downgrade
    return JsonResponse({
        "success": True,
        "is_upgrade": False,
        "current_plan": current_sub.subscription.name,
        "new_plan": new_plan.name,
        "current_price": str(current_sub.subscription.price),
        "new_price": str(new_plan.price),
        "effective_date": current_sub.expires_at.strftime("%B %d, %Y"),
    })


def _handle_downgrade(request, current_sub, new_plan):
    """
    SAFE DOWNGRADE FLOW (AUTHORIZE.NET)

    - Create NEW subscription starting next billing date
    - Cancel CURRENT subscription so it does not renew
    - Current plan remains active until expiry
    - Prevent double billing
    """

    if not current_sub.authorize_subscription_id:
        return JsonResponse(
            {"error": "No active subscription found."},
            status=400
        )

    start_date = (
        current_sub.expires_at.date()
        if current_sub.expires_at
        else timezone.now().date()
    )

    # Determine interval
    if new_plan.billing_type == "monthly":
        interval_length = 1
        interval_unit = "months"
    elif new_plan.billing_type == "yearly":
        interval_length = 12
        interval_unit = "months"
    else:
        interval_length = 1
        interval_unit = "months"

    try:
        with transaction.atomic():

            #  Create NEW subscription (starts next billing)
            create_payload = {
                "ARBCreateSubscriptionRequest": {
                    "merchantAuthentication": {
                        "name": settings.AUTHORIZE_NET_LOGIN_ID,
                        "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
                    },
                    "subscription": {
                        "name": f"{new_plan.name} (Scheduled)",
                        "paymentSchedule": {
                            "interval": {
                                "length": interval_length,
                                "unit": interval_unit,
                            },
                            "startDate": start_date.isoformat(),
                            "totalOccurrences": "9999",
                        },
                        "amount": str(new_plan.price),
                        "profile": {
                            "customerProfileId": current_sub.customer_profile_id,
                            "customerPaymentProfileId": current_sub.customer_payment_profile_id,
                        },
                    },
                }
            }

            create_resp = requests.post(
                settings.AUTHORIZE_NET_ENDPOINT,
                json=create_payload,
                timeout=30,
            )
            create_data = parse_authorize_response(create_resp)

            if create_data.get("messages", {}).get("resultCode") != "Ok":
                raise Exception(f"Scheduled subscription creation failed: {create_data}")

            new_subscription_id = create_data.get("subscriptionId")
            if not new_subscription_id:
                raise Exception("No subscription ID returned from Authorize.Net")

            #  Cancel CURRENT subscription (prevents renewal)
            cancel_payload = {
                "ARBCancelSubscriptionRequest": {
                    "merchantAuthentication": {
                        "name": settings.AUTHORIZE_NET_LOGIN_ID,
                        "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
                    },
                    "subscriptionId": current_sub.authorize_subscription_id,
                }
            }

            cancel_resp = requests.post(
                settings.AUTHORIZE_NET_ENDPOINT,
                json=cancel_payload,
                timeout=30,
            )
            cancel_data = parse_authorize_response(cancel_resp)

            if cancel_data.get("messages", {}).get("resultCode") != "Ok":
                raise Exception("Failed to cancel current subscription")

            # Update local DB state
            current_sub.cancel_at_period_end = True
            current_sub.pending_subscription = new_plan
            current_sub.pending_authorize_subscription_id = new_subscription_id
            current_sub.save(
                update_fields=[
                    "cancel_at_period_end",
                    "pending_subscription",
                    "pending_authorize_subscription_id",
                ]
            )

            return JsonResponse({
                "success": True,
                "downgrade": True,
                "message": (
                    f"Your plan will change to {new_plan.name} "
                    f"on {current_sub.expires_at.strftime('%B %d, %Y')}. "
                    f"You will continue to enjoy {current_sub.subscription.name} "
                    f"until then."
                ),
                "effective_date": current_sub.expires_at.isoformat(),
            })

    except Exception as e:
        logger.exception("Downgrade failed")
        return JsonResponse(
            {"error": "Unable to downgrade subscription. Please contact support."},
            status=500
        )



@login_required
@require_POST
def cancel_plan_change(request):
    """
    Cancel a pending downgrade and cancel the scheduled subscription in Authorize.Net
    """
    
    current_sub = UserSubscription.objects.filter(
        user=request.user,
        active=True
    ).first()

    if not current_sub or not current_sub.pending_subscription:
        return JsonResponse({"error": "No pending plan change"}, status=400)

    # Cancel the pending subscription in Authorize.Net
    if current_sub.pending_authorize_subscription_id:
        try:
            cancel_payload = {
                "ARBCancelSubscriptionRequest": {
                    "merchantAuthentication": {
                        "name": settings.AUTHORIZE_NET_LOGIN_ID,
                        "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
                    },
                    "subscriptionId": current_sub.pending_authorize_subscription_id,
                }
            }
            
            logger.info(f"Cancelling pending subscription: {current_sub.pending_authorize_subscription_id}")
            
            cancel_resp = requests.post(
                settings.AUTHORIZE_NET_ENDPOINT,
                json=cancel_payload,
                timeout=30,
            )
            
            cancel_resp.encoding = "utf-8-sig"
            cancel_data = json.loads(cancel_resp.text)
            
            logger.info(f"Cancel response: {json.dumps(cancel_data, indent=2)}")
            
            # Check response
            result = cancel_data.get("ARBCancelSubscriptionResponse", cancel_data)
            messages = result.get("messages", {})
            
            if messages.get("resultCode") != "Ok":
                error_messages = messages.get("message", [])
                if isinstance(error_messages, dict):
                    error_messages = [error_messages]
                
                error_text = "; ".join(
                    f"{m.get('code', 'N/A')}: {m.get('text', 'Unknown error')}"
                    for m in error_messages
                ) if error_messages else "Failed to cancel subscription in Authorize.Net"
                
                logger.error(f"Failed to cancel pending subscription: {error_text}")
                
                return JsonResponse({
                    "error": f"Failed to cancel pending subscription: {error_text}"
                }, status=400)
            
            logger.info(f"✓ Successfully cancelled pending subscription in Authorize.Net: {current_sub.pending_authorize_subscription_id}")
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {str(e)}")
            return JsonResponse({"error": "Invalid response from payment gateway"}, status=500)
        except requests.RequestException as e:
            logger.error(f"Request error: {str(e)}")
            return JsonResponse({"error": "Failed to connect to payment gateway"}, status=500)
        except Exception as e:
            logger.exception("Failed to cancel pending subscription")
            return JsonResponse({"error": str(e)}, status=500)

    # Clear pending subscription from database
    current_sub.pending_subscription = None
    current_sub.pending_authorize_subscription_id = None
    current_sub.save(update_fields=['pending_subscription', 'pending_authorize_subscription_id'])

    return JsonResponse({
        "success": True,
        "message": "Plan change cancelled successfully. The scheduled subscription has been cancelled in Authorize.Net."
    })
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
from dashboard.utils.id_encoder import decode_id, encode_id
from django.http import HttpResponseNotFound
import html
from dashboard.utils.roles import is_admin
from decimal import Decimal

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

from adminpanel.models import Subscription
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
    username = request.POST.get("username")
    email = request.POST.get("email")
    password = request.POST.get("password")

    if not all([username, email, password]):
        messages.error(request, "All fields are required.")
        return redirect("checkout", subscription_id=subscription.id)

    # Block existing active subscription
    existing_user = User.objects.filter(email=email).first()
    if existing_user and UserSubscription.objects.filter(
        user=existing_user, active=True
    ).exists():
        messages.error(
            request,
            "This email already has an active subscription. Please log in."
        )
        return redirect("checkout", subscription_id=subscription.id)

    request.session["pending_user"] = {
        "username": username,
        "email": email,
        "password": password,
    }

    data_value = request.POST.get("dataValue")
    data_descriptor = request.POST.get("dataDescriptor")

    if not data_value or not data_descriptor:
        messages.error(request, "Payment token missing. Please try again.")
        return redirect("checkout", subscription_id=subscription.id)

    pending_user = request.session.get("pending_user")
    if not pending_user:
        messages.error(request, "Session expired. Please start again.")
        return redirect("register")

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
                    "email": pending_user["email"],
                    "paymentProfiles": {
                        "customerType": "individual",
                        "billTo": {
                            "firstName": pending_user["username"],
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

        print("=" * 80)
        print("CREATING CUSTOMER PROFILE...")
        print("=" * 80)

        profile_response = requests.post(
            settings.AUTHORIZE_NET_ENDPOINT,
            json=profile_payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=60,
        )

        if profile_response.status_code != 200:
            print(f"❌ Profile Creation HTTP Error: {profile_response.status_code}")
            messages.error(request, f"Payment gateway error (HTTP {profile_response.status_code}).")
            return redirect("checkout", subscription_id=subscription.id)

        raw_profile_text = profile_response.content.decode("utf-8-sig").strip()
        print("RAW PROFILE RESPONSE:")
        print(raw_profile_text)

        # Parse profile response
        if raw_profile_text.startswith("{"):
            profile_data = json.loads(raw_profile_text)
        elif raw_profile_text.startswith("<"):
            profile_data = xmltodict.parse(raw_profile_text)
        else:
            messages.error(request, "Invalid profile response format.")
            return redirect("checkout", subscription_id=subscription.id)

        print("PARSED PROFILE DATA:")
        print(json.dumps(profile_data, indent=2, default=str))

        # Extract profile response
        profile_result = profile_data.get("createCustomerProfileResponse", profile_data)
        
        if profile_result.get("messages", {}).get("resultCode") != "Ok":
            error_messages = profile_result.get("messages", {}).get("message", [])
            if isinstance(error_messages, list):
                error_msg = error_messages[0].get("text", "Profile creation failed")
            else:
                error_msg = error_messages.get("text", "Profile creation failed")
            
            print(f"❌ Profile Creation Failed: {error_msg}")
            messages.error(request, f"Payment setup failed: {error_msg}")
            return redirect("checkout", subscription_id=subscription.id)

        # Extract Customer Profile and Payment Profile IDs
        customer_profile_id = profile_result.get("customerProfileId")
        customer_payment_profile_id_list = profile_result.get("customerPaymentProfileIdList", [])
        
        if isinstance(customer_payment_profile_id_list, list) and len(customer_payment_profile_id_list) > 0:
            customer_payment_profile_id = customer_payment_profile_id_list[0]
        elif isinstance(customer_payment_profile_id_list, str):
            customer_payment_profile_id = customer_payment_profile_id_list
        else:
            customer_payment_profile_id = None

        print(f"✓ Customer Profile ID: {customer_profile_id}")
        print(f"✓ Payment Profile ID: {customer_payment_profile_id}")

        if not customer_profile_id or not customer_payment_profile_id:
            print("❌ Missing profile IDs")
            messages.error(request, "Payment profile creation incomplete.")
            return redirect("checkout", subscription_id=subscription.id)

    except Exception as e:
        print(f"❌ Profile Creation Error: {e}")
        import traceback
        traceback.print_exc()
        messages.error(request, "Unable to create payment profile.")
        return redirect("checkout", subscription_id=subscription.id)

    # -------------------------------------------------
    # STEP 2: Create ARB Subscription using Customer Profile
    # -------------------------------------------------
    payload = {
        "ARBCreateSubscriptionRequest": {
            "merchantAuthentication": {
                "name": settings.AUTHORIZE_NET_LOGIN_ID,
                "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
            },
            "refId": f"ref{timezone.now().timestamp()}",
            "subscription": {
                "name": f"{subscription.name} Subscription",
                "paymentSchedule": {
                    "interval": {
                        "length": 12 if subscription.billing_type == "yearly" else 1,
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

    # -------------------------------------------------
    # Send request to Authorize.Net
    # -------------------------------------------------
    try:
        print("=" * 80)
        print("CREATING ARB SUBSCRIPTION...")
        print("=" * 80)
        
        response = requests.post(
            settings.AUTHORIZE_NET_ENDPOINT,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=60,
        )

        if response.status_code != 200:
            print(f"❌ HTTP Error: {response.status_code}")
            messages.error(request, f"Payment gateway error (HTTP {response.status_code}).")
            return redirect("checkout", subscription_id=subscription.id)

        raw_text = response.content.decode("utf-8-sig").strip()
        
        print("RAW AUTHORIZE.NET RESPONSE:")
        print(raw_text)

        # Parse JSON or XML
        if raw_text.startswith("{"):
            parsed_data = json.loads(raw_text)
        elif raw_text.startswith("<"):
            parsed_data = xmltodict.parse(raw_text)
        else:
            messages.error(request, "Unexpected response format.")
            return redirect("checkout", subscription_id=subscription.id)

        print("PARSED SUBSCRIPTION DATA:")
        print(json.dumps(parsed_data, indent=2, default=str))

    except Exception as e:
        print(f"❌ Subscription Creation Error: {e}")
        import traceback
        traceback.print_exc()
        messages.error(request, f"Subscription creation failed: {e}")
        return redirect("checkout", subscription_id=subscription.id)

    # -------------------------------------------------
    # Handle response
    # -------------------------------------------------
    subscription_response = parsed_data.get("ARBCreateSubscriptionResponse", parsed_data)

    if not subscription_response:
        messages.error(request, "Invalid response structure.")
        return redirect("checkout", subscription_id=subscription.id)

    response_messages = subscription_response.get("messages", {})
    result_code = response_messages.get("resultCode")
    
    if result_code != "Ok":
        message_obj = response_messages.get("message", {})
        
        if isinstance(message_obj, list):
            error_msg = message_obj[0].get("text", "Unknown error")
        else:
            error_msg = message_obj.get("text", "Unknown error")
        
        print(f"❌ Subscription Failed: {error_msg}")
        messages.error(request, f"Subscription failed: {error_msg}")
        return redirect("checkout", subscription_id=subscription.id)

    authorize_subscription_id = subscription_response.get("subscriptionId")
    
    if not authorize_subscription_id:
        messages.error(request, "Subscription ID missing.")
        return redirect("checkout", subscription_id=subscription.id)

    print(f"✓ Success! Subscription ID: {authorize_subscription_id}")

    # -------------------------------------------------
    # Create user
    # -------------------------------------------------
    try:
        user = User.objects.create_user(
            username=pending_user["username"],
            email=pending_user["email"],
            password=pending_user["password"],
        )
        print(f"✓ User created: {user.username}")
    except Exception as e:
        print(f"❌ User creation error: {e}")
        messages.error(request, "Account creation failed.")
        return redirect("register")

    # -------------------------------------------------
    # Save subscription + payment WITH PROFILE IDs
    # -------------------------------------------------
    try:
        UserSubscription.objects.create(
            user=user,
            subscription=subscription,
            active=True,
            authorize_subscription_id=authorize_subscription_id,
            customer_profile_id=customer_profile_id,
            customer_payment_profile_id=customer_payment_profile_id,
        )
        print("✓ UserSubscription created with profile IDs")

        Payment.objects.create(
            user=user,
            subscription=subscription,
            amount=subscription.price,
            transaction_id=authorize_subscription_id,
            status="success",
            response_code="OK",
        )
        print("✓ Payment record created")

    except Exception as e:
        print(f"❌ Database error: {e}")
        import traceback
        traceback.print_exc()
        messages.error(request, "Error saving subscription.")
        return redirect("checkout", subscription_id=subscription.id)

    request.session.pop("pending_user", None)

    messages.success(
        request,
        "Subscription activated successfully! You can now log in."
    )
    return redirect("login")
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

    # ✅ THIS IS THE KEY PART
    has_successful_payment = False
    if active_subscription:
        has_successful_payment = Payment.objects.filter(
            user=request.user,
            subscription=active_subscription.subscription,
            status="success",
        ).exists()

    available_plan = Subscription.objects.first()

    all_plans = []
    if active_subscription:
        all_plans = Subscription.objects.filter(
            status='published',
            billing_type=active_subscription.subscription.billing_type
        ).order_by('price')

    return render(request, "settings.html", {
        "user": request.user,
        "active_subscription": active_subscription,
        "has_successful_payment": has_successful_payment, 
        "next_billing_date": next_billing_date,
        "payments": payments,
        "started_at": timezone.now(),
        "available_plan": available_plan,
        "all_plans": all_plans,
    })

import logging
logger = logging.getLogger(__name__)

def resume_subscription(request):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request"}, status=400)

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
        return JsonResponse(
            {"error": "No subscription to resume"},
            status=400
        )

    #  We MUST have stored profile IDs
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

    #  SAFE start date → prevents double charge
    if not subscription.expires_at:
        return JsonResponse(
            {"error": "Subscription expiry date missing"},
            status=400
        )

    start_date = subscription.expires_at.date()

    # ------------------------------------------------
    # Create NEW Authorize.Net subscription
    # ------------------------------------------------
    # Determine interval for Authorize.Net
    # NOTE: Authorize.Net only accepts "days" or "months", not "years"
    if subscription.subscription.billing_type == "monthly":
        interval_length = 1
        interval_unit = "months"
    elif subscription.subscription.billing_type == "yearly":
        interval_length = 12
        interval_unit = "months"
    else:
        # Default to monthly
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
                    "startDate": start_date.isoformat(),
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

        logger.info(f"Response status: {resp.status_code}")
        
        resp.encoding = "utf-8-sig"
        data = json.loads(resp.text)

        logger.info(f"Authorize.Net RESUME response: {json.dumps(data, indent=2)}")

        # Handle response with or without wrapper
        result = data.get("ARBCreateSubscriptionResponse", data)
        messages = result.get("messages", {})

        if messages.get("resultCode") != "Ok":
            error_messages = messages.get("message", [])
            
            # Handle both list and single dict
            if isinstance(error_messages, dict):
                error_messages = [error_messages]
            
            if error_messages:
                error_text = "; ".join(
                    f"{m.get('code', 'N/A')}: {m.get('text', 'Unknown error')}"
                    for m in error_messages
                )
            else:
                error_text = f"Unknown payment gateway error. Full response: {json.dumps(data)}"

            logger.error(f"Authorize.Net resume failed: {error_text}")

            return JsonResponse(
                {"error": f"Unable to resume subscription: {error_text}"},
                status=400
            )

        new_subscription_id = result.get("subscriptionId")

        if not new_subscription_id:
            logger.error(f"No subscription ID in response. Full response: {json.dumps(data)}")
            return JsonResponse(
                {"error": "No subscription ID returned from payment gateway"},
                status=400
            )

        logger.info(f"✓ Successfully created new subscription: {new_subscription_id}")

    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {str(e)}")
        return JsonResponse(
            {"error": f"Invalid response from payment gateway: {str(e)}"},
            status=500
        )
    except requests.RequestException as e:
        logger.error(f"Request error: {str(e)}")
        return JsonResponse(
            {"error": f"Failed to connect to payment gateway: {str(e)}"},
            status=500
        )
    except Exception as e:
        logger.exception("Resume subscription failed")
        return JsonResponse({"error": str(e)}, status=500)

    # ------------------------------------------------
    # Update LOCAL DB
    # ------------------------------------------------
    subscription.authorize_subscription_id = new_subscription_id
    subscription.cancel_at_period_end = False
    subscription.save(
        update_fields=[
            "authorize_subscription_id",
            "cancel_at_period_end",
        ]
    )

    logger.info(f"✓ Subscription resumed successfully for user {request.user.username}")

    return JsonResponse({
        "success": True,
        "message": (
            "Your subscription has been resumed. "
            "Billing will continue after the current period ends."
        )
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
    #  GET → show page
    if request.method == "GET":
        return render(request, "index3.html")

    #  POST → MULTI file AJAX upload
    if request.method == "POST":
        audio_files = request.FILES.getlist("audio_file")
        video_files = request.FILES.getlist("video_file")

        files = audio_files + video_files

        if not files:
            return JsonResponse({"error": "No files uploaded"}, status=400)

        created_recordings = []

        for file in files:
            # detect type
            file_type = "audio" if "audio" in file.content_type else "video"

            # create DB row
            recording = Recording.objects.create(
                user=request.user,
                title=f"Recording {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
                status="uploading",
                file_type=file_type,
            )

            # upload to S3
            s3_key = f"uploads/{file_type}/{recording.id}_{file.name}"
            s3_url = upload_to_s3(file, s3_key)

            # update recording
            recording.audio_url = s3_url
            recording.status = "processing"
            recording.progress = 10
            recording.save()

            #  enqueue background job (ONE TASK PER FILE)
            process_recording_task.delay(recording.id)

            created_recordings.append(recording.id)

        return JsonResponse({
            "success": True,
            "recording_ids": created_recordings,
            "count": len(created_recordings)
        })

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

@login_required
@require_POST
def change_subscription_plan(request):
    """
    Handle upgrade/downgrade subscription
    - Immediate upgrade with prorated charge
    - Downgrade scheduled for next billing cycle
    """
    
    try:
        data = json.loads(request.body)
        new_subscription_id = data.get('subscription_id')
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request data"}, status=400)

    if not new_subscription_id:
        return JsonResponse({"error": "Subscription ID required"}, status=400)

    # Get current subscription
    current_sub = UserSubscription.objects.filter(
        user=request.user,
        active=True
    ).select_related('subscription').first()

    if not current_sub:
        return JsonResponse({"error": "No active subscription found"}, status=400)

    # Get new subscription plan
    try:
        new_plan = AdminSubscription.objects.get(id=new_subscription_id, status='published')
    except AdminSubscription.DoesNotExist:
        return JsonResponse({"error": "Invalid subscription plan"}, status=400)

    # Check if it's the same plan
    if current_sub.subscription.id == new_plan.id:
        return JsonResponse({"error": "You are already on this plan"}, status=400)

    # Check if billing types match
    if current_sub.subscription.billing_type != new_plan.billing_type:
        return JsonResponse(
            {"error": "Cannot switch between monthly and yearly plans. Please cancel and resubscribe."},
            status=400
        )

    # Determine if upgrade or downgrade
    is_upgrade = new_plan.price > current_sub.subscription.price

    # Check if we have profile IDs
    if not current_sub.customer_profile_id or not current_sub.customer_payment_profile_id:
        return JsonResponse(
            {"error": "Payment profile missing. Please contact support."},
            status=400
        )

    if is_upgrade:
        # UPGRADE: Immediate prorated charge + update subscription
        return _handle_upgrade_prorated(request, current_sub, new_plan)
    else:
        # DOWNGRADE: Schedule for next billing cycle
        return _handle_downgrade(request, current_sub, new_plan)


def _calculate_prorated_amount(current_sub, new_plan):
    """
    Calculate prorated amount for upgrade
    """
    if not current_sub.started_at or not current_sub.expires_at:
        return new_plan.price
    
    # Calculate time remaining in current billing cycle
    now = timezone.now()
    total_period = (current_sub.expires_at - current_sub.started_at).total_seconds()
    time_remaining = (current_sub.expires_at - now).total_seconds()
    
    if total_period <= 0 or time_remaining <= 0:
        return new_plan.price
    
    # Calculate prorated amounts
    time_used_ratio = Decimal(str((total_period - time_remaining) / total_period))
    
    # Credit from old plan (unused portion)
    old_plan_credit = current_sub.subscription.price * (Decimal('1') - time_used_ratio)
    
    # Prorated charge for new plan (remaining time at new rate)
    new_plan_prorated = new_plan.price * (Decimal('1') - time_used_ratio)
    
    # Amount to charge now = new plan prorated - old plan credit
    prorated_charge = new_plan_prorated - old_plan_credit
    
    # Ensure charge is at least $0
    prorated_charge = max(prorated_charge, Decimal('0'))
    
    logger.info(f"Proration calculation:")
    logger.info(f"  Time used ratio: {time_used_ratio}")
    logger.info(f"  Old plan credit: ${old_plan_credit}")
    logger.info(f"  New plan prorated: ${new_plan_prorated}")
    logger.info(f"  Prorated charge: ${prorated_charge}")
    
    return prorated_charge


def _handle_upgrade_prorated(request, current_sub, new_plan):
    """
    Handle upgrade with prorated billing
    """
    
    # Calculate prorated amount
    prorated_amount = _calculate_prorated_amount(current_sub, new_plan)
    
    try:
        # STEP 1: Charge prorated amount as one-time transaction
        if prorated_amount > 0:
            charge_payload = {
                "createTransactionRequest": {
                    "merchantAuthentication": {
                        "name": settings.AUTHORIZE_NET_LOGIN_ID,
                        "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
                    },
                    "transactionRequest": {
                        "transactionType": "authCaptureTransaction",
                        "amount": str(round(prorated_amount, 2)),
                        "profile": {
                            "customerProfileId": current_sub.customer_profile_id,
                            "paymentProfile": {
                                "paymentProfileId": current_sub.customer_payment_profile_id
                            }
                        },
                        "lineItems": {
                            "lineItem": {
                                "itemId": "UPGRADE",
                                "name": f"Upgrade to {new_plan.name}",
                                "description": "Prorated upgrade charge",
                                "quantity": "1",
                                "unitPrice": str(round(prorated_amount, 2))
                            }
                        }
                    }
                }
            }
            
            logger.info(f"Charging prorated amount: ${prorated_amount}")
            
            charge_resp = requests.post(
                settings.AUTHORIZE_NET_ENDPOINT,
                json=charge_payload,
                timeout=30,
            )
            charge_resp.encoding = "utf-8-sig"
            charge_data = json.loads(charge_resp.text)
            
            logger.info(f"Charge response: {json.dumps(charge_data, indent=2)}")
            
            # Check charge response
            charge_result = charge_data.get("transactionResponse", {})
            response_code = charge_result.get("responseCode")
            
            if response_code != "1":  # 1 = Approved
                error_text = charge_result.get("errors", [{}])[0].get("errorText", "Charge failed")
                logger.error(f"Prorated charge failed: {error_text}")
                return JsonResponse({
                    "error": f"Upgrade charge failed: {error_text}"
                }, status=400)
            
            transaction_id = charge_result.get("transId")
            logger.info(f"✓ Prorated charge successful: ${prorated_amount}, Transaction ID: {transaction_id}")
            
            # Record payment
            Payment.objects.create(
                user=request.user,
                subscription=new_plan,
                amount=prorated_amount,
                transaction_id=transaction_id,
                status="success",
                response_code="OK",
            )
        else:
            logger.info("No prorated charge needed (amount is $0 or negative)")
        
        # STEP 2: Update existing ARB subscription amount
        update_payload = {
            "ARBUpdateSubscriptionRequest": {
                "merchantAuthentication": {
                    "name": settings.AUTHORIZE_NET_LOGIN_ID,
                    "transactionKey": settings.AUTHORIZE_NET_TRANSACTION_KEY,
                },
                "subscriptionId": current_sub.authorize_subscription_id,
                "subscription": {
                    "name": f"{new_plan.name}",
                    "amount": str(new_plan.price),
                }
            }
        }
        
        logger.info(f"Updating subscription to new amount: ${new_plan.price}")
        
        update_resp = requests.post(
            settings.AUTHORIZE_NET_ENDPOINT,
            json=update_payload,
            timeout=30,
        )
        update_resp.encoding = "utf-8-sig"
        update_data = json.loads(update_resp.text)
        
        logger.info(f"Update response: {json.dumps(update_data, indent=2)}")
        
        # Check update response
        update_result = update_data.get("ARBUpdateSubscriptionResponse", update_data)
        update_messages = update_result.get("messages", {})
        
        if update_messages.get("resultCode") != "Ok":
            error_messages = update_messages.get("message", [])
            if isinstance(error_messages, dict):
                error_messages = [error_messages]
            
            error_text = "; ".join(
                f"{m.get('code', 'N/A')}: {m.get('text', 'Unknown error')}"
                for m in error_messages
            ) if error_messages else "Update failed"
            
            logger.error(f"Subscription update failed: {error_text}")
            return JsonResponse({
                "error": f"Subscription update failed: {error_text}. You were charged ${prorated_amount} but the subscription wasn't updated. Please contact support."
            }, status=400)
        
        logger.info(f"✓ Subscription updated successfully")
        
        # STEP 3: Update database
        current_sub.subscription = new_plan
        current_sub.save(update_fields=['subscription'])
        
        logger.info(f"✓ Upgraded successfully to {new_plan.name}")
        
        return JsonResponse({
            "success": True,
            "message": f"Successfully upgraded to {new_plan.name}! You were charged ${round(prorated_amount, 2)} for the remaining period. Your next billing will be ${new_plan.price} on {current_sub.expires_at.strftime('%B %d, %Y')}.",
            "upgrade": True,
            "prorated_charge": str(round(prorated_amount, 2))
        })

    except Exception as e:
        logger.exception("Upgrade failed")
        return JsonResponse({"error": str(e)}, status=500)


def _handle_downgrade(request, current_sub, new_plan):
    """
    Handle downgrade - create new subscription scheduled for next billing cycle
    """
    
    start_date = current_sub.expires_at.date() if current_sub.expires_at else timezone.now().date()
    
    # Determine interval for Authorize.Net
    if new_plan.billing_type == "monthly":
        interval_length = 1
        interval_unit = "months"
    elif new_plan.billing_type == "yearly":
        interval_length = 12
        interval_unit = "months"
    else:
        interval_length = 1
        interval_unit = "months"

    # Create new ARB subscription at lower price (starts at next billing cycle)
    payload = {
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

    logger.info(f"Downgrade payload: {json.dumps(payload, indent=2)}")

    try:
        # Create new subscription
        create_resp = requests.post(
            settings.AUTHORIZE_NET_ENDPOINT,
            json=payload,
            timeout=30,
        )
        create_resp.encoding = "utf-8-sig"
        create_data = json.loads(create_resp.text)

        logger.info(f"Downgrade create response: {json.dumps(create_data, indent=2)}")

        result = create_data.get("ARBCreateSubscriptionResponse", create_data)
        messages = result.get("messages", {})

        if messages.get("resultCode") != "Ok":
            error_messages = messages.get("message", [])
            if isinstance(error_messages, dict):
                error_messages = [error_messages]
            
            error_text = "; ".join(
                f"{m.get('code', 'N/A')}: {m.get('text', 'Unknown error')}"
                for m in error_messages
            ) if error_messages else "Unknown error"

            logger.error(f"Downgrade failed: {error_text}")
            return JsonResponse({"error": f"Downgrade failed: {error_text}"}, status=400)

        new_subscription_id = result.get("subscriptionId")

        if not new_subscription_id:
            return JsonResponse({"error": "No subscription ID returned"}, status=400)

        # Store the new subscription ID and pending plan
        current_sub.pending_subscription = new_plan
        current_sub.pending_authorize_subscription_id = new_subscription_id
        current_sub.save(update_fields=['pending_subscription', 'pending_authorize_subscription_id'])

        logger.info(f"✓ Downgrade scheduled to {new_plan.name}, new ARB ID: {new_subscription_id}")

        return JsonResponse({
            "success": True,
            "message": f"Your plan will change to {new_plan.name} (${new_plan.price}/{new_plan.billing_type}) on your next billing date: {current_sub.expires_at.strftime('%B %d, %Y')}. You'll continue to enjoy {current_sub.subscription.name} benefits until then.",
            "downgrade": True,
            "effective_date": current_sub.expires_at.isoformat()
        })

    except Exception as e:
        logger.exception("Downgrade failed")
        return JsonResponse({"error": str(e)}, status=500)


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
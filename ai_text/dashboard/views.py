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
from django.utils import timezone
# import language_tool_python  # ✅ Added for grammar analysis
from difflib import ndiff
import html

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.http import JsonResponse

from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.shortcuts import redirect
from django.core.paginator import Paginator
from pydub.silence import detect_silence
from .utils.analyse import transcribe_audio_with_timestamps, analyze_filler_words_from_text, analyze_pacing, analyze_grammar_with_claude_sync, generate_pdf


import asyncio
import json
from typing import Dict, List
from .utils.claude_utils import ask_claude_for_segments_with_timestamps
import whisper
from .utils.s3_bucket import upload_to_s3

from .tasks import process_recording_task

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


# PRACTICE TAB (default dashboard)
@login_required
def practice_view(request):
    return render(request, "index3.html")

@login_required
def recording_status(request, recording_id):
    rec = Recording.objects.get(id=recording_id, user=request.user)
    return JsonResponse({
        "status": rec.status,
        "progress": rec.progress,
        "duration": rec.duration,
        "pdf_url": rec.pdf_url,
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


@login_required
def settings_view(request):
      return render(request, "settings.html", {
        "user": request.user
    })

@login_required
def delete_recordings(request):
    if request.method == "POST":
        selected_ids = request.POST.getlist('selected_ids')
        Recording.objects.filter(id__in=selected_ids, user=request.user).delete()
    return redirect('recording')   
    
@login_required(login_url='login')
def recording_detail(request, rec_id):
    """Display detailed analysis of a recording"""
    rec = get_object_or_404(Recording, pk=rec_id, user=request.user)
    
    # Initialize variables
    grammar_inline = ""
    grammar_data = {}
    grammar_results = []
    conciseness = 0
    speaking_tips = []

    conciseness_data = rec.conciseness_data or {}
    
    # Calculate conciseness
    if rec.filler_data and rec.pacing_data:
        fillers_per_min = rec.filler_data.get('fillers_per_minute', 0)
        conciseness = max(0, min(100, 100 - (fillers_per_min * 10)))

    # ✅ Grammar should ALWAYS run if transcript exists
    if rec.transcript:
        grammar_data = analyze_grammar_with_claude_sync(rec.transcript)
        grammar_results = grammar_data.get("analysis", [])
        grammar_inline = grammar_data.get("inline", "")

        rec.grammar_data = grammar_results
        rec.save(update_fields=["grammar_data"])

    
    # Generate personalized speaking tips
    speaking_tips = generate_speaking_tips(rec.filler_data, rec.pacing_data, grammar_data)
    filler_frequency = {}
    fillers_per_minute = 0

    if rec.filler_data:
        filler_frequency = rec.filler_data.get("filler_frequency", {})
        fillers_per_minute = rec.filler_data.get("fillers_per_minute", 0)
        conciseness = max(0, min(100, 100 - (fillers_per_min * 10)))

    segments = []
    pace_segments = []
    if isinstance(rec.pacing_segments, dict):
        segments = rec.pacing_segments.get("segments", [])
        pace_segments = rec.pacing_segments.get("pace_segments", [])

    pacing_analysis = analyze_pacing(
        rec.transcript,
        rec.duration
    ) 

    talk_time_data = calculate_talk_time_percentage(
        segments=segments,
        total_duration=rec.duration
    )

    summary_data = rec.summary_data

    if rec.transcript and not summary_data:
        try:
            summary_data = generate_transcript_summary_with_claude(rec.transcript)
            rec.summary_data = summary_data
            rec.save(update_fields=["summary_data"])
        except Exception as e:
            print("Summary generation failed:", e)
            summary_data = {"summary": [], "actions": []}

    context = {
        'rec': rec,
        "segments": segments,
        'pace_segments': pace_segments,
        'grammar_inline': grammar_inline,
        'grammar_data': grammar_data,
        'grammar_results': grammar_results,
        'conciseness': conciseness,
        'speaking_tips': speaking_tips,

        "talk_time": talk_time_data,
        'filler_frequency': filler_frequency,
        'fillers_per_minute': fillers_per_minute,
        'conciseness_data': conciseness_data,
        'pacing_analysis': pacing_analysis,
        "summary_data": summary_data,
    }

   
    return render(request, 'recording_detail.html', context)

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
    if request.method == "POST":
        username = request.POST.get("username")
        email = request.POST.get("email")
        password = request.POST.get("password")

        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already exists!")
            return redirect("register")

        user = User.objects.create_user(username=username, email=email, password=password)
        user.save()
        messages.success(request, "Account created successfully! Please log in.")
        return redirect("login")

    return render(request, "register.html")

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
        if user is not None:
            login(request, user)
            return redirect("practice")
        else:
            messages.error(request, "Invalid username or password.")
            return redirect("login")

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



# -------------------------------------------------------------------
# ✅ AWS S3 HELPERS
# -------------------------------------------------------------------


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




# -------------------------------------------------------------------
# ✅ ANALYSIS HELPERS
# -------------------------------------------------------------------












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




@login_required(login_url='login')
# -------------------------------------------------------------------
# ✅ MAIN VIEW — HANDLES AUDIO + VIDEO
# -------------------------------------------------------------------
# def speech_tx(request):
#     transcript = ""
#     video_transcript = ""
#     pdf_url = ""
#     s3_audio_url = ""
#     s3_video_url = ""
#     filler_analysis = {}
#     pacing_analysis = {}
#     pacing_segments = []
#     grammar_results = []
#     grammar_inline = ""
#     grammar_data = {}
#     audio_duration = 0
     
#     claude_seg_result = {"segments": [], "wpm": 0} 
#     USE_S3 = bool(getattr(settings, "AWS_STORAGE_BUCKET_NAME", ""))

#     # -------------------- MANUAL & AUTO CORRECTION --------------------
#     if request.method == "POST" and request.POST.get("action") == "apply_corrections":
#         corrected_text = request.POST.get("corrected_text", "")
#         pdf_path = generate_pdf(corrected_text)
#         if USE_S3:
#             pdf_key = f"uploads/pdf/corrected_{int(time.time())}.pdf"
#             with open(pdf_path, "rb") as pdf_file:
#                 pdf_url = upload_to_s3(pdf_file, pdf_key)
#         else:
#             pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
#             os.makedirs(pdf_dir, exist_ok=True)
#             final_path = os.path.join(pdf_dir, f"corrected_{int(time.time())}.pdf")
#             os.rename(pdf_path, final_path)
#             pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(final_path)}"
#         return render(request, "index3.html", {
#             "transcript": corrected_text,
#             "pdf_url": pdf_url,
#             "message": "✅ Grammar corrections applied!"
#         })

#     if request.method == "POST" and request.POST.get("action") == "auto_correct_grammar":
#         original_text = request.POST.get("original_text", "")
#         corrected_text = apply_grammar_corrections(original_text)
#         pdf_path = generate_pdf(corrected_text)
#         pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
#         os.makedirs(pdf_dir, exist_ok=True)
#         final_path = os.path.join(pdf_dir, f"auto_corrected_{int(time.time())}.pdf")
#         os.rename(pdf_path, final_path)
#         pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(final_path)}"
#         return render(request, "index3.html", {
#             "transcript": corrected_text,
#             "pdf_url": pdf_url,
#             "message": "✅ Auto-correct applied!"
#         })

#     # -------------------- AUDIO UPLOAD --------------------
#     audio_file = None
#     file_format = None
#     audio_processing_enabled = False
#     if request.method == "POST" and request.FILES.get("audio_file"):
#         audio_file = request.FILES["audio_file"]
#         file_format = audio_file.name.split('.')[-1].lower()
#         audio_processing_enabled = True

#     if audio_processing_enabled:
#         if file_format not in SUPPORTED_AUDIO_FORMATS:
#             return render(request, "index3.html", {
#                 "error": f"Unsupported audio format: .{file_format}. Supported formats: {', '.join(SUPPORTED_AUDIO_FORMATS)}"
#             })

#         # -------------------- S3 --------------------
#         if USE_S3:
#             s3_audio_key = f"uploads/audio/{int(time.time())}_{audio_file.name}"
#             s3_audio_url = upload_to_s3(audio_file, s3_audio_key)
#             temp_original = tempfile.NamedTemporaryFile(delete=False, suffix=f".{file_format}").name
#             temp_wav = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
#             download_from_s3(s3_audio_key, temp_original)
#             sound = AudioSegment.from_file(temp_original)
#             sound = sound.set_channels(1).set_frame_rate(16000)
#             sound.export(temp_wav, format="wav")
#             audio_duration = len(sound) / 1000.0
#             timestamped_segments = transcribe_audio_with_timestamps(temp_wav)
#             claude_seg_result = ask_claude_for_segments_with_timestamps(timestamped_segments, audio_duration)
#             transcript = " ".join([s["text"] for s in timestamped_segments])
#             filler_analysis = analyze_filler_words_from_text(transcript, audio_duration)
#             pacing_analysis = analyze_pacing(transcript, audio_duration)
#             final_transcript = transcript
#             grammar_data = analyze_grammar_with_claude_sync(final_transcript)
#             grammar_results = grammar_data.get("analysis", [])
#             grammar_inline = grammar_data.get("inline", "")
#             pdf_path = generate_pdf(transcript)
#             pdf_key = f"uploads/pdf/{os.path.basename(pdf_path)}"
#             with open(pdf_path, "rb") as f:
#                 pdf_url = upload_to_s3(f, pdf_key)
#             os.remove(temp_original)
#             os.remove(temp_wav)
#             os.remove(pdf_path)

#         # -------------------- LOCAL --------------------
#         else:
#             local_audio_dir = os.path.join(settings.MEDIA_ROOT, "audio")
#             os.makedirs(local_audio_dir, exist_ok=True)
#             temp_original_path = os.path.join(local_audio_dir, f"temp_{int(time.time())}.{file_format}")
#             with open(temp_original_path, "wb") as f:
#                 for chunk in audio_file.chunks():
#                     f.write(chunk)
#             local_audio_path = os.path.join(local_audio_dir, f"converted_{int(time.time())}.wav")
#             sound = AudioSegment.from_file(temp_original_path, format=file_format)
#             sound = sound.set_channels(1).set_frame_rate(16000)
#             sound.export(local_audio_path, format="wav")
#             audio_duration = len(sound) / 1000.0
#             timestamped_segments = transcribe_audio_with_timestamps(local_audio_path)
#             filler_analysis = analyze_filler_words_from_text(transcript, audio_duration)
#             claude_seg_result = ask_claude_for_segments_with_timestamps(timestamped_segments, audio_duration)
#             transcript = " ".join([s["text"] for s in timestamped_segments])
#             pacing_analysis = analyze_pacing(transcript, audio_duration)
#             final_transcript = transcript
#             grammar_data = analyze_grammar_with_claude_sync(final_transcript)
#             grammar_results = grammar_data.get("analysis", [])
#             grammar_inline = grammar_data.get("inline", "")
#             pdf_path = generate_pdf(transcript)
#             local_pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
#             os.makedirs(local_pdf_dir, exist_ok=True)
#             final_pdf_path = os.path.join(local_pdf_dir, os.path.basename(pdf_path))
#             os.rename(pdf_path, final_pdf_path)
#             s3_audio_url = settings.MEDIA_URL + f"audio/{os.path.basename(local_audio_path)}"
#             pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(final_pdf_path)}"

#     # -------------------- VIDEO UPLOAD --------------------
#     if request.method == "POST" and request.FILES.get("video_file"):
#         video_file = request.FILES["video_file"]
#         video_format = video_file.name.split('.')[-1].lower()
#         if USE_S3:
#             s3_video_key = f"uploads/video/{video_file.name}"
#             s3_video_url = upload_to_s3(video_file, s3_video_key)
#             temp_video = tempfile.NamedTemporaryFile(delete=False, suffix=f".{video_format}").name
#             download_from_s3(s3_video_key, temp_video)
#             clip = VideoFileClip(temp_video)
#             temp_audio = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
#             clip.audio.write_audiofile(temp_audio, codec="pcm_s16le")
#             clip.close()
#             sound = AudioSegment.from_file(temp_audio)
#             sound = sound.set_channels(1).set_frame_rate(16000)
#             sound.export(temp_audio, format="wav")
#             audio_duration = len(sound) / 1000.0
#             timestamped_segments = transcribe_audio_with_timestamps(temp_audio)
#             claude_seg_result = ask_claude_for_segments_with_timestamps(timestamped_segments, audio_duration)
#             video_transcript = " ".join([s["text"] for s in timestamped_segments])
#             final_transcript = video_transcript
#             filler_analysis = analyze_filler_words_from_text(video_transcript, audio_duration)
#             hedging_analysis = analyze_hedging_words(video_transcript, audio_duration)
#             conciseness_data = analyze_conciseness(video_transcript, audio_duration)
#             pacing_analysis = analyze_pacing(video_transcript, audio_duration)
#             grammar_data = analyze_grammar_with_claude_sync(final_transcript)
#             grammar_results = grammar_data.get("analysis", [])
#             grammar_inline = grammar_data.get("inline", "")
#             pdf_path = generate_pdf(video_transcript)
#             pdf_key = f"uploads/pdf/{os.path.basename(pdf_path)}"
#             with open(pdf_path, "rb") as f:
#                 pdf_url = upload_to_s3(f, pdf_key)
#             os.remove(temp_video)
#             os.remove(temp_audio)
#             os.remove(pdf_path)

#         else:
#             vid_dir = os.path.join(settings.MEDIA_ROOT, "video")
#             os.makedirs(vid_dir, exist_ok=True)
#             local_video_path = os.path.join(vid_dir, video_file.name)
#             with open(local_video_path, "wb") as f:
#                 for chunk in video_file.chunks():
#                     f.write(chunk)
#             clip = VideoFileClip(local_video_path)
#             temp_wav = local_video_path.replace(f".{video_format}", ".wav")
#             clip.audio.write_audiofile(temp_wav, codec="pcm_s16le")
#             clip.close()
#             sound = AudioSegment.from_file(temp_wav)
#             sound = sound.set_channels(1).set_frame_rate(16000)
#             sound.export(temp_wav, format="wav")
#             audio_duration = len(sound) / 1000.0
#             timestamped_segments = transcribe_audio_with_timestamps(temp_wav)
#             claude_seg_result = ask_claude_for_segments_with_timestamps(timestamped_segments, audio_duration)
#             video_transcript = " ".join([s["text"] for s in timestamped_segments])
#             filler_analysis = analyze_filler_words_from_text(video_transcript, audio_duration)
#             hedging_analysis = analyze_hedging_words(video_transcript, audio_duration)
#             conciseness_data = analyze_conciseness(video_transcript, audio_duration)
#             # filler_analysis = analyze_filler_words(video_transcript, audio_duration)
#             final_transcript = video_transcript
#             pacing_analysis = analyze_pacing(video_transcript, audio_duration)
#             grammar_data = analyze_grammar_with_claude_sync(final_transcript)
#             grammar_results = grammar_data.get("analysis", [])
#             grammar_inline = grammar_data.get("inline", "")
#             pdf_path = generate_pdf(video_transcript)
#             pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
#             os.makedirs(pdf_dir, exist_ok=True)
#             final_pdf_path = os.path.join(pdf_dir, os.path.basename(pdf_path))
#             os.rename(pdf_path, final_pdf_path)
#             pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(final_pdf_path)}"

#     # -------------------- FINAL SEGMENTS & DATABASE --------------------
#     raw_segments = claude_seg_result["segments"] if 'claude_seg_result' in locals() else []
#     pacing_segments = adjust_segments_to_audio(raw_segments, audio_duration)
#     final_transcript = transcript or video_transcript
#     hedging_analysis = analyze_hedging_words(final_transcript, audio_duration)
#     conciseness_data = analyze_conciseness(final_transcript, audio_duration)

    
#     pace_segments = []

#     for seg in pacing_segments:
#         words = len(seg["text"].split())
#         duration = max(seg["end_s"] - seg["start_s"], 0.1)
#         wpm = (words / duration) * 60
#         pace_label = classify_pace_segment(wpm)

#         pace_segments.append({
#             "label": pace_label,
#             "start_s": seg["start_s"],
#             "end_s": seg["end_s"],
#             "start": seg["start"],
#             "end": seg["end"],
#             "text": seg["text"],
#             "wpm": round(wpm)
#         })
#     pacing_segments = mark_pause_segments(pacing_segments)
#     # Build filler word list from detected frequencies
#     filler_list = list(filler_analysis.get("filler_frequency", {}).keys())

#     for seg in pacing_segments:
#         words = len(seg["text"].split())
#         duration = seg["end_s"] - seg["start_s"]
#         wpm = (words / duration) * 60 if duration else 0

#         seg["label"] = classify_pace_segment(wpm)

#         # ✅ highlight filler words
#         seg["text"] = highlight_filler_words(
#             seg["text"],
#             FILLER_WORDS,
#             seg["start_s"],
#             seg["end_s"]
#         )

#         # ✅ highlight hedging words (NEW)
#         seg["text"] = highlight_hedging_words(
#             seg["text"],
#             HEDGING_WORDS,
#             seg["start_s"],
#             seg["end_s"]
#         )


#     # Apply filler-word highlighting using CLAUDE filler list
    

#     pacing_analysis = {"wpm": claude_seg_result.get("wpm", 0), **pacing_analysis}

#     if request.user.is_authenticated and (transcript or video_transcript):
#         Recording.objects.create(
#             user=request.user,
#             title=f"Recording - {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
#             audio_url=s3_audio_url or s3_video_url,
#             pdf_url=pdf_url,
#             transcript=transcript or video_transcript,
#             filler_data=filler_analysis,
#             pacing_data=pacing_analysis,
#             pacing_segments={
#                 "segments": pacing_segments,
#                 "pace_segments": pace_segments
#             },
#             grammar_data=grammar_data.get("analysis", []),
#             duration=audio_duration,
#             hedging_data=hedging_analysis,
#             conciseness_data=conciseness_data
#         )


#     return render(request, "index3.html", {
#         "transcript": transcript,
#         "video_transcript": video_transcript,
#         "s3_audio_url": s3_audio_url or s3_video_url,
#         "pdf_url": pdf_url,
#         "filler_analysis": filler_analysis,
#         "pacing_analysis": pacing_analysis,
#         "pacing_segments": pacing_segments,
#         "grammar_results": grammar_results,
#         "grammar_inline": grammar_inline,
#         "grammar_data": grammar_data,
#     })

def speech_tx(request):
    # ✅ GET → show page
    if request.method == "GET":
        return render(request, "index3.html")

    # ✅ POST → AJAX upload
    if request.method == "POST":
        file = request.FILES.get("audio_file") or request.FILES.get("video_file")

        if not file:
            return JsonResponse({"error": "No file uploaded"}, status=400)

        file_type = "audio" if "audio" in file.content_type else "video"

        recording = Recording.objects.create(
            user=request.user,
            title=f"Recording {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
            status="uploading",
            file_type=file_type,
        )

        s3_key = f"uploads/{file_type}/{recording.id}_{file.name}"
        s3_url = upload_to_s3(file, s3_key)

        recording.audio_url = s3_url
        recording.status = "processing"
        recording.progress = 10
        recording.save()

        process_recording_task.delay(recording.id)

        return JsonResponse({
            "success": True,
            "recording_id": recording.id,
            "status": recording.status
        })

    # fallback (should not hit)
    return JsonResponse({"error": "Method not allowed"}, status=405)
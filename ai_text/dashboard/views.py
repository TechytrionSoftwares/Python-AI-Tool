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
import language_tool_python  # ✅ Added for grammar analysis
from difflib import ndiff
import html

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from django.contrib.auth.decorators import login_required


from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.shortcuts import redirect
from django.core.paginator import Paginator
from pydub.silence import detect_silence


import asyncio
import json
from typing import Dict, List
from .utils.claude_utils import ask_claude_for_segments_with_timestamps
import whisper

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
@login_required(login_url='login')
def practice_view(request):
    return render(request, 'index3.html')


# RECORDING TAB (shows list)
@login_required
def recording_view(request):
    recordings = Recording.objects.filter(user=request.user).order_by('-created_at')
    paginator = Paginator(recordings, 10)  # 10 per page
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    return render(request, 'recording.html', {'recordings': page_obj})

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
    
    # Process grammar data
    if rec.transcript:
        if rec.grammar_data and isinstance(rec.grammar_data, list) and len(rec.grammar_data) > 0:
            grammar_results = rec.grammar_data
            grammar_data = {'issues': len(grammar_results), 'analysis': grammar_results}
            grammar_inline = generate_inline_html_from_claude(rec.transcript, grammar_results)
        else:
            try:
                grammar_data = analyze_grammar_with_claude_sync(rec.transcript)
                grammar_inline = grammar_data.get('inline', '')
                grammar_results = grammar_data.get('analysis', [])
                rec.grammar_data = grammar_results
                rec.save(update_fields=['grammar_data'])
            except Exception as e:
                print(f"Error: {e}")
                grammar_data = {'issues': 0, 'analysis': []}
    
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
    }

   
    return render(request, 'recording_detail.html', context)

    # def generate_inline_html_from_stored_data(original_text: str, grammar_results: list) -> str:
    #     """
    #     Generate inline HTML from previously stored grammar results.
    #     Used when displaying existing recordings.
    #     """
    #     if not grammar_results or not original_text:
    #         return html.escape(original_text) if original_text else ""
        
    #     result = original_text
    #     positioned_errors = []
        
    #     # Convert stored grammar data to positioned errors
    #     for error in grammar_results:
    #         if isinstance(error, dict):
    #             # Handle both Claude format and language_tool format
    #             original = error.get('original', '') or error.get('context', '')
                
    #             # Extract correction from various possible formats
    #             correction = error.get('correction', '')
    #             if not correction and error.get('suggestions'):
    #                 suggestions = error.get('suggestions')
    #                 if isinstance(suggestions, list) and suggestions:
    #                     correction = suggestions[0]
                
    #             error_type = error.get('type', 'grammar')
    #             explanation = error.get('explanation', '') or error.get('issue', '')
                
    #             # Only process if we have both original and correction
    #             if original and correction and original in result:
    #                 pos = result.find(original)
    #                 if pos != -1:
    #                     positioned_errors.append({
    #                         'pos': pos,
    #                         'original': original,
    #                         'correction': correction,
    #                         'type': error_type,
    #                         'explanation': explanation
    #                     })
        
    #     # Sort by position (reverse order to avoid offset issues)
    #     positioned_errors.sort(key=lambda x: x['pos'], reverse=True)
        
    #     # Apply replacements from end to start
    #     for error in positioned_errors:
    #         original_text_part = error['original']
    #         correction = error['correction']
    #         error_type = error['type']
    #         explanation = error['explanation']
    #         pos = error['pos']
            
    #         # Create the replacement HTML
    #         replacement = (
    #             f"<span title='{error_type}: {explanation}'>"
    #             f"<del style='color:#d93025;text-decoration:line-through;'>{html.escape(original_text_part)}</del>"
    #             f"<ins style='color:#1a8917;font-weight:bold;'> {html.escape(correction)}</ins>"
    #             f"</span>"
    #         )
            
    #         # Replace at specific position
    #         result = result[:pos] + replacement + result[pos + len(original_text_part):]
        
    # return result


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

def analyze_filler_words_from_text(transcript: str, duration: float):
    if not transcript:
        return {
            "total_fillers": 0,
            "fillers_per_minute": 0,
            "filler_frequency": {}
        }

    text = transcript.lower()
    found = []

    for word in FILLER_WORDS:
        pattern = r"\b" + re.escape(word) + r"\b"
        matches = re.findall(pattern, text)
        found.extend([word] * len(matches))

    counter = Counter(found)
    minutes = max(duration / 60, 1)

    return {
        "total_fillers": sum(counter.values()),
        "fillers_per_minute": round(sum(counter.values()) / minutes, 2),
        "filler_frequency": dict(counter)
    }

# -------------------------------------------------------------------
# ✅ AWS S3 HELPERS
# -------------------------------------------------------------------
def upload_to_s3(file_obj, key):
    """Upload a file object to AWS S3 and return its public URL."""
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME
    )
    s3.upload_fileobj(file_obj, settings.AWS_STORAGE_BUCKET_NAME, key)
    return f"https://{settings.AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com/{key}"


def download_from_s3(key, local_path):
    """Download a file from S3 to local path."""
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME
    )
    s3.download_file(settings.AWS_STORAGE_BUCKET_NAME, key, local_path)

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
# ✅ UPDATED — PDF GENERATION (with analysis sections)
# -------------------------------------------------------------------
def generate_pdf(transcript_text, filler_analysis=None, pacing_analysis=None, grammar_inline=None):
    """
    Generate a detailed PDF report that includes filler analysis, pacing, and grammar highlights.
    """
    temp_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    doc = SimpleDocTemplate(temp_pdf.name, pagesize=letter,
                            rightMargin=50, leftMargin=50, topMargin=50, bottomMargin=50)
    styles = getSampleStyleSheet()
    story = []

    # --- Title ---
    title_style = styles["Heading2"]
    title_style.textColor = colors.HexColor("#1A237E")
    story.append(Paragraph("Speech-to-Text Report", title_style))
    story.append(Spacer(1, 0.3 * inch))

    # --- Filler Word Analysis ---
    if filler_analysis:
        story.append(Paragraph("<b>Filler Word Analysis</b>", styles["Heading3"]))
        story.append(Paragraph(f"Total: {filler_analysis['total_fillers']}", styles["Normal"]))
        story.append(Paragraph(f"Per Minute: {filler_analysis['fillers_per_minute']}", styles["Normal"]))
        for word, count in filler_analysis["filler_frequency"].items():
            story.append(Paragraph(f"{word} × {count}", styles["Normal"]))
        story.append(Spacer(1, 0.3 * inch))

    # --- Speaking Pace ---
    if pacing_analysis:
        story.append(Paragraph("<b>Speaking Pace</b>", styles["Heading3"]))
        story.append(Paragraph(f"WPM: {pacing_analysis['wpm']}", styles["Normal"]))
        story.append(Paragraph(pacing_analysis["pace_feedback"], styles["Normal"]))
        story.append(Spacer(1, 0.3 * inch))

    # --- Grammar & Clarity Suggestions (inline markup supported) ---
    if grammar_inline:
        story.append(Paragraph("<b>Grammar & Clarity Suggestions</b>", styles["Heading3"]))

        # Convert your inline HTML to reportlab-safe markup
        grammar_for_pdf = (
            grammar_inline
            .replace("<span style=\"color:red;text-decoration:line-through;\">", "<font color='red'><strike>")
            .replace("<span style=\"color:green;font-weight:bold;\">", "<font color='green'><b>")
            .replace("</span>", "</b></font>")
            .replace("</strike></font>", "</strike></font>")
        )

        # Split into chunks to avoid text cutoff for long strings
        chunks = grammar_for_pdf.split("<br>")
        for chunk in chunks:
            story.append(Paragraph(chunk, styles["Normal"]))
            story.append(Spacer(1, 0.1 * inch))
        story.append(Spacer(1, 0.3 * inch))

    # --- Transcript ---
    story.append(Paragraph("<b>Full Transcript</b>", styles["Heading3"]))
    for para in transcript_text.strip().split("\n"):
        story.append(Paragraph(para, styles["Normal"]))
        story.append(Spacer(1, 0.2 * inch))

    doc.build(story)
    return temp_pdf.name

    """Generate a detailed analysis PDF including filler words, pacing, grammar, and transcript."""
    temp_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")

    doc = SimpleDocTemplate(
        temp_pdf.name,
        pagesize=letter,
        rightMargin=50, leftMargin=50, topMargin=50, bottomMargin=50
    )
    styles = getSampleStyleSheet()
    story = []

    # ---------- Title ----------
    title_style = styles["Heading1"]
    title_style.textColor = colors.HexColor("#1A237E")
    story.append(Paragraph("🎙 Speech Analysis Report", title_style))
    story.append(Spacer(1, 0.3 * inch))

    # ---------- Filler Word Analysis ----------
    if filler_analysis:
        story.append(Paragraph("<b>🗣️ Filler Word Analysis</b>", styles["Heading3"]))
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph(f"Total: {filler_analysis.get('total_fillers', 0)}", styles["Normal"]))
        story.append(Paragraph(f"Per Minute: {filler_analysis.get('fillers_per_minute', 0):.2f}", styles["Normal"]))

        if filler_analysis.get("filler_frequency"):
            for word, count in filler_analysis["filler_frequency"].items():
                story.append(Paragraph(f"{word}: ×{count}", styles["Normal"]))
        story.append(Spacer(1, 0.2 * inch))

    # ---------- Speaking Pace ----------
    if pacing_analysis:
        story.append(Paragraph("<b>⏱️ Speaking Pace</b>", styles["Heading3"]))
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph(f"WPM: {pacing_analysis.get('wpm', 0):.2f}", styles["Normal"]))
        story.append(Paragraph(pacing_analysis.get("pace_feedback", ""), styles["Normal"]))
        story.append(Spacer(1, 0.2 * inch))

    # ---------- Grammar Suggestions ----------
    if grammar_inline:
        story.append(Paragraph("<b>📝 Grammar & Clarity Suggestions</b>", styles["Heading3"]))
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph(
            grammar_inline,
            ParagraphStyle(
                "Grammar",
                parent=styles["Normal"],
                textColor=colors.black,
                allowOrphans=1
            )
        ))
        story.append(Spacer(1, 0.3 * inch))

    # ---------- Transcript ----------
    story.append(Paragraph("<b>📜 Full Transcript</b>", styles["Heading3"]))
    story.append(Spacer(1, 0.1 * inch))
    body_style = styles["Normal"]
    body_style.fontSize = 12
    body_style.leading = 18
    story.append(Paragraph(transcript_text.replace("\n", "<br/>"), body_style))

    doc.build(story)
    return temp_pdf.name

def analyze_conciseness(transcript: str, duration_sec: float):
    """
    Detect repeated phrases/sentences that reduce conciseness
    """
    if not transcript:
        return {
            "repetition_count": 0,
            "repetitions": [],
            "feedback": "Great! No repetition detected."
        }

    text = transcript.lower()
    sentences = re.split(r'[.!?]', text)

    phrase_counter = Counter()
    repeated_phrases = []

    for s in sentences:
        words = s.strip().split()
        if len(words) >= 4:
            phrase = " ".join(words[:6])  # first few words
            phrase_counter[phrase] += 1

    for phrase, count in phrase_counter.items():
        if count >= 2:
            repeated_phrases.append({
                "phrase": phrase,
                "count": count
            })

    return {
        "repetition_count": len(repeated_phrases),
        "repetitions": repeated_phrases,
        "feedback": (
            "You repeated some ideas. Try to say things once, clearly."
            if repeated_phrases else
            "There were 0 moments where you could have been more concise."
        )
    }

# -------------------------------------------------------------------
# ✅ SPEECH RECOGNITION
# -------------------------------------------------------------------
# def transcribe_audio_any(input_path):
#     """Convert any audio/video format and transcribe chunk by chunk."""
#     recognizer = sr.Recognizer()

#     # Convert video to wav if needed
#     if input_path.lower().endswith(('.mp4', '.mov', '.mkv', '.avi', '.webm', '.flv')):
#         temp_audio_path = input_path + "_audio.wav"
#         clip = VideoFileClip(input_path)
#         clip.audio.write_audiofile(temp_audio_path, codec="pcm_s16le")
#         clip.close()
#         audio_path = temp_audio_path
#     else:
#         # Convert audio to wav
#         ext = input_path.split('.')[-1]
#         audio_path = input_path.replace(f".{ext}", ".wav")
#         sound = AudioSegment.from_file(input_path)
#         sound = sound.set_channels(1)
#         sound = sound.set_frame_rate(16000)
#         sound.export(audio_path, format="wav")

#     audio = AudioSegment.from_wav(audio_path)
#     chunk_ms = 30000  # 30 seconds
#     transcript_parts = []

#     for i, start in enumerate(range(0, len(audio), chunk_ms)):
#         chunk = audio[start:start+chunk_ms]
#         chunk_file = f"{audio_path}_chunk_{i}.wav"
#         chunk.export(chunk_file, format="wav")

#         with sr.AudioFile(chunk_file) as source:
#             audio_data = recognizer.record(source)

#         try:
#             text = recognizer.recognize_google(audio_data)
#             transcript_parts.append(text)
#         except Exception:
#             transcript_parts.append("[Unrecognized segment]")

#         os.remove(chunk_file)

#     return " ".join(transcript_parts)


# -------------------------------------------------------------------
# ✅ ANALYSIS HELPERS
# -------------------------------------------------------------------

def analyze_hedging_words(transcript: str, duration_sec: float):
    if not transcript:
        return {
            "total_hedges": 0,
            "hedges_per_minute": 0,
            "hedge_frequency": {}
        }

    text = transcript.lower()
    found = []

    for word in HEDGING_WORDS:
        pattern = r"\b" + re.escape(word) + r"\b"
        matches = re.findall(pattern, text)
        found.extend([word] * len(matches))

    counter = Counter(found)
    minutes = max(duration_sec / 60, 1)

    return {
        "total_hedges": sum(counter.values()),
        "hedges_per_minute": round(sum(counter.values()) / minutes, 2),
        "hedge_frequency": dict(counter)
    }

def highlight_hedging_words(text, hedging_words, start_s=None, end_s=None):
    if not hedging_words:
        return text

    for word in hedging_words:
        pattern = rf"\b({re.escape(word)})\b"
        replacement = (
            rf'<span class="hedging-word" '
            rf'data-start="{start_s}" '
            rf'data-end="{end_s}" '
            rf'data-word="\1">\1</span>'
        )
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    return text


def analyze_pacing(transcript, audio_duration):
    if not transcript or audio_duration <= 0:
        return {"wpm": 0, "pace_feedback": "Insufficient data for pacing analysis."}

    word_count = len(transcript.split())
    minutes = audio_duration / 60.0
    wpm = round(word_count / minutes, 2)

    if wpm < 125:
        feedback = "🟡 You're speaking a bit slowly. Try increasing your pace slightly."
    elif wpm > 160:
        feedback = "🔴 You're speaking quite fast. Slow down a bit for better clarity."
    else:
        feedback = "🟢 Great! Your speaking pace is within the ideal range (125–160 WPM)."

    return {"wpm": wpm, "pace_feedback": feedback}

def generate_inline_grammar_html(text):
    """Return Grammarly-style inline HTML with red strikethroughs and green insertions."""
    if not text.strip():
        return ""

    tool = language_tool_python.LanguageTool('en-US')
    matches = tool.check(text)
    corrected_text = language_tool_python.utils.correct(text, matches)

    # Compare word-by-word diff
    diff = ndiff(text.split(), corrected_text.split())
    html_output = ""
    for word in diff:
        if word.startswith("- "):
            html_output += f"<del style='color:#d93025;text-decoration:line-through;'>{html.escape(word[2:])}</del> "
        elif word.startswith("+ "):
            html_output += f"<ins style='color:#1a8917;font-weight:bold;'>{html.escape(word[2:])}</ins> "
        else:
            html_output += html.escape(word[2:]) + " "
    return html_output.strip()
# ✅ Improved — Grammar & Clarity Analysis
def analyze_grammar(text):
    if not text.strip():
        return []

    tool = language_tool_python.LanguageTool('en-US')
    matches = tool.check(text)

    results = []
    for match in matches[:10]:  # Show up to 10 grammar issues
        results.append({
            "issue": match.message,
            "context": match.context,
            "suggestions": match.replacements[:3] if match.replacements else []
        })
    return results


# ✅ NEW — Automatically apply grammar corrections
def apply_grammar_corrections(text):
    if not text.strip():
        return text
    tool = language_tool_python.LanguageTool('en-US')
    corrected_text = tool.correct(text)
    return corrected_text

def grammar_analysis(text):
    """Return corrected text + inline HTML with red/green markup."""
    if not text.strip():
        return {"corrected": text, "inline": text, "issues": 0}

    tool = language_tool_python.LanguageTool('en-US')
    matches = tool.check(text)
    corrected_text = tool.correct(text)

    annotated = text
    offset = 0

    for m in matches:
        wrong = annotated[m.offset + offset : m.offset + offset + m.errorLength]
        suggestion = m.replacements[0] if m.replacements else ""

        if suggestion:
            html = (
                f"<span style='color:red;text-decoration:line-through;'>{wrong}</span>"
                f"<span style='color:green;font-weight:bold;'> {suggestion}</span>"
            )
        else:
            html = f"<span style='color:red;text-decoration:line-through;'>{wrong}</span>"

        annotated = (
            annotated[: m.offset + offset] +
            html +
            annotated[m.offset + offset + m.errorLength :]
        )

        offset += len(html) - len(wrong)

    return {
        "corrected": corrected_text,
        "inline": annotated,
        "issues": len(matches)
    }
def analyze_grammar_with_claude_sync(text: str) -> Dict:
    """Claude AI grammar analysis - synchronous version for Django"""
    if not text.strip():
        return {"corrected": text, "inline": text, "issues": 0, "analysis": []}
    
    if not CLAUDE_AVAILABLE:
        return fallback_grammar_analysis(text)
    
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
    if not api_key:
        return fallback_grammar_analysis(text)
    
    try:
        client = anthropic.Anthropic(api_key=api_key)
        
        prompt = f"""
        Analyze this TRANSCRIBED SPOKEN LANGUAGE for issues a listener would NOTICE.

        IMPORTANT CONTEXT:
        - This is SPOKEN audio/video, not written text
        - Ignore punctuation, commas, and capitalization completely
        - Ignore filler words (handled elsewhere)
        - Treat the transcript as raw speech

        YOU ARE ALLOWED TO:
        - Fix true SPOKEN grammar errors
        - Remove IMMEDIATELY repeated sentences or phrases
        (only when the same idea is repeated back-to-back with no new meaning)

        YOU ARE NOT ALLOWED TO:
        - Suggest punctuation (commas, periods, quotes, etc.)
        - Rewrite sentences for style or flow
        - Improve wording unless repetition is removed
        - Merge or restructure sentences
        - Rephrase for clarity beyond repetition removal

        CHECK FOR THESE ISSUES ONLY:
        1. Missing articles (a / an / the)
        2. Subject–verb agreement
        3. Incorrect verb tense
        4. Singular / plural errors
        5. Incorrect pronouns (only if clearly wrong)
        6. Immediate repetition that hurts understanding

        IMPORTANT RULES ABOUT REPETITION:
        - Repetition counts ONLY if the same sentence or idea appears back-to-back
        - Do NOT remove repetition if it is separated by other content
        - Do NOT rewrite the remaining sentence
        - Simply remove the repeated portion

        Transcript:
        \"\"\"{text}\"\"\"

        Return ONLY valid JSON (no explanations outside JSON):
        {{
        "corrected_text": "Corrected spoken transcript",
        "errors": [
            {{
            "original": "exact phrase or sentence from transcript",
            "correction": "spoken correction or removal",
            "type": "article|verb|tense|plural|pronoun|clarity",
            "explanation": "why this affects spoken understanding"
            }}
        ]
        }}
        """

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        
        response_text = message.content[0].text.strip()
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()
        
        result = json.loads(response_text)
        raw_errors = result.get('errors', [])

        # ✅ FILTER invalid / useless errors
        clean_errors = []
        for e in raw_errors:
            original = e.get("original", "").strip()
            correction = e.get("correction", "").strip()

            if (
                original
                and correction
                and original != correction
                and original in text
            ):
                clean_errors.append(e)

        inline_html = generate_inline_html_from_claude(text, clean_errors)

        
        corrected_text = result.get("corrected_text", text)

        return {
            "corrected": corrected_text,
            "inline": inline_html if clean_errors else html.escape(corrected_text),
            "issues": len(clean_errors),
            "analysis": clean_errors,
            "has_correction": corrected_text.strip() != text.strip()
        }
    except Exception as e:
        print(f"Claude Error: {e}")
        return fallback_grammar_analysis(text)

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


def generate_inline_html_from_claude(original_text: str, errors: List[Dict]) -> str:
    """
    Generate inline HTML with Claude grammar & clarity corrections.
    Handles repeated sentences and multi-occurrence fixes correctly.
    """

    if not errors:
        return html.escape(original_text)

    result = original_text
    positioned_errors = []

    for error in errors:
        original = error.get("original", "").strip()
        correction = error.get("correction", "").strip()

        if not original or not correction or original == correction:
            continue

        # 🔥 FIND ALL OCCURRENCES (CRITICAL FIX)
        for match in re.finditer(re.escape(original), result):
            positioned_errors.append({
                "pos": match.start(),
                "end": match.end(),
                "original": original,
                "correction": correction,
                "type": error.get("type", "grammar"),
                "explanation": error.get("explanation", "")
            })

    # 🔥 Replace from END to START (prevents index shifting)
    positioned_errors.sort(key=lambda x: x["pos"], reverse=True)

    for err in positioned_errors:
        replacement = (
            f"<span class='grammar-fix' "
            f"title='{html.escape(err['type'])}: {html.escape(err['explanation'])}'>"
            f"<del style='color:#d93025;'>{html.escape(err['original'])}</del>"
            f"<ins style='color:#1a8917;font-weight:bold;'> {html.escape(err['correction'])}</ins>"
            f"</span>"
        )

        result = (
            result[:err["pos"]] +
            replacement +
            result[err["end"]:]
        )

    return result


def fallback_grammar_analysis(text: str) -> Dict:
    """Fallback using language_tool_python"""
    try:
        import language_tool_python
        tool = language_tool_python.LanguageTool('en-US')
        matches = tool.check(text)
        corrected_text = tool.correct(text)
        
        errors = []
        for match in matches[:10]:
            errors.append({
                "original": text[match.offset:match.offset + match.errorLength],
                "correction": match.replacements[0] if match.replacements else "",
                "type": "grammar",
                "explanation": match.message[:100]
            })
        
        return {
            "corrected": corrected_text,
            "inline": generate_inline_html_from_claude(text, errors),
            "issues": len(matches),
            "analysis": errors
        }
    except:
        return {"corrected": text, "inline": html.escape(text), "issues": 0, "analysis": []}

def adjust_segments_to_audio(segments, audio_duration):
    """
    Adjust Claude's segments to match the exact audio duration.

    Args:
        segments (list): List of segments from Claude, each with start_s and end_s.
        audio_duration (float): Actual audio duration in seconds.

    Returns:
        list: Updated segments with corrected start_s and end_s.
    """
    if not segments:
        return []

    # Calculate estimated total duration from Claude segments
    estimated_total = segments[-1]['end_s']

    # Calculate ratio to stretch/shrink segments to match real audio
    duration_ratio = audio_duration / estimated_total if estimated_total > 0 else 1.0

    adjusted_segments = []
    for seg in segments:
        start_s = seg['start_s'] * duration_ratio
        end_s = seg['end_s'] * duration_ratio

        # Convert back to MM:SS format
        start_mmss = f"{int(start_s // 60):02d}:{int(start_s % 60):02d}"
        end_mmss = f"{int(end_s // 60):02d}:{int(end_s % 60):02d}"

        adjusted_segments.append({
            "start": start_mmss,
            "end": end_mmss,
            "start_s": round(start_s, 2),
            "end_s": round(end_s, 2),
            "text": seg['text']
        })

    return adjusted_segments

def transcribe_audio_with_timestamps(audio_path):
    """
    Returns list of {"start_s": float, "end_s": float, "text": str}
    """
    model = whisper.load_model("tiny")
    result = model.transcribe(audio_path, word_timestamps=True)
    segments = []
    for seg in result["segments"]:
        segments.append({
            "start_s": seg["start"],
            "end_s": seg["end"],
            "text": seg["text"].strip()
        })
    return segments

def highlight_filler_words(text, filler_words, start_s=None, end_s=None):
    """
    Wrap filler words with clickable span including timestamps
    """
    if not filler_words:
        return text

    for word in filler_words:
        pattern = rf"\b({re.escape(word)})\b"
        replacement = (
            rf'<span class="filler-word" '
            rf'data-start="{start_s}" '
            rf'data-end="{end_s}" '
            rf'data-word="\1">\1</span>'
        )
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    return text



def mark_pause_segments(segments, pause_threshold_ms=400):
    """
    Marks transcript segments that likely contain hesitation pauses.
    """
    for seg in segments:
        duration_ms = (seg["end_s"] - seg["start_s"]) * 1000

        if duration_ms >= pause_threshold_ms:
            seg["has_pause"] = True
            seg["pause_ms"] = int(duration_ms)
        else:
            seg["has_pause"] = False

    return segments

def calculate_confidence_score(filler_data, pacing_data):
    score = 100

    pauses_per_min = filler_data.get("fillers_per_minute", 0)
    wpm = pacing_data.get("wpm", 0)

    score -= pauses_per_min * 8

    if wpm < 120 or wpm > 170:
        score -= 10

    return max(0, min(100, int(score)))

def classify_pace_segment(wpm):
    if wpm >= 190:
        return "Fast"
    elif wpm >= 165:
        return "Slightly Fast"
    elif wpm >= 125:
        return "Good"
    else:
        return "Slow"


@login_required(login_url='login')
# -------------------------------------------------------------------
# ✅ MAIN VIEW — HANDLES AUDIO + VIDEO
# -------------------------------------------------------------------
def speech_tx(request):
    transcript = ""
    video_transcript = ""
    pdf_url = ""
    s3_audio_url = ""
    s3_video_url = ""
    filler_analysis = {}
    pacing_analysis = {}
    pacing_segments = []
    grammar_results = []
    grammar_inline = ""
    grammar_data = {}
    audio_duration = 0
     
    claude_seg_result = {"segments": [], "wpm": 0} 
    USE_S3 = bool(getattr(settings, "AWS_STORAGE_BUCKET_NAME", ""))

    # -------------------- MANUAL & AUTO CORRECTION --------------------
    if request.method == "POST" and request.POST.get("action") == "apply_corrections":
        corrected_text = request.POST.get("corrected_text", "")
        pdf_path = generate_pdf(corrected_text)
        if USE_S3:
            pdf_key = f"uploads/pdf/corrected_{int(time.time())}.pdf"
            with open(pdf_path, "rb") as pdf_file:
                pdf_url = upload_to_s3(pdf_file, pdf_key)
        else:
            pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
            os.makedirs(pdf_dir, exist_ok=True)
            final_path = os.path.join(pdf_dir, f"corrected_{int(time.time())}.pdf")
            os.rename(pdf_path, final_path)
            pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(final_path)}"
        return render(request, "index3.html", {
            "transcript": corrected_text,
            "pdf_url": pdf_url,
            "message": "✅ Grammar corrections applied!"
        })

    if request.method == "POST" and request.POST.get("action") == "auto_correct_grammar":
        original_text = request.POST.get("original_text", "")
        corrected_text = apply_grammar_corrections(original_text)
        pdf_path = generate_pdf(corrected_text)
        pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
        os.makedirs(pdf_dir, exist_ok=True)
        final_path = os.path.join(pdf_dir, f"auto_corrected_{int(time.time())}.pdf")
        os.rename(pdf_path, final_path)
        pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(final_path)}"
        return render(request, "index3.html", {
            "transcript": corrected_text,
            "pdf_url": pdf_url,
            "message": "✅ Auto-correct applied!"
        })

    # -------------------- AUDIO UPLOAD --------------------
    audio_file = None
    file_format = None
    audio_processing_enabled = False
    if request.method == "POST" and request.FILES.get("audio_file"):
        audio_file = request.FILES["audio_file"]
        file_format = audio_file.name.split('.')[-1].lower()
        audio_processing_enabled = True

    if audio_processing_enabled:
        if file_format not in SUPPORTED_AUDIO_FORMATS:
            return render(request, "index3.html", {
                "error": f"Unsupported audio format: .{file_format}. Supported formats: {', '.join(SUPPORTED_AUDIO_FORMATS)}"
            })

        # -------------------- S3 --------------------
        if USE_S3:
            s3_audio_key = f"uploads/audio/{int(time.time())}_{audio_file.name}"
            s3_audio_url = upload_to_s3(audio_file, s3_audio_key)
            temp_original = tempfile.NamedTemporaryFile(delete=False, suffix=f".{file_format}").name
            temp_wav = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
            download_from_s3(s3_audio_key, temp_original)
            sound = AudioSegment.from_file(temp_original)
            sound = sound.set_channels(1).set_frame_rate(16000)
            sound.export(temp_wav, format="wav")
            audio_duration = len(sound) / 1000.0
            timestamped_segments = transcribe_audio_with_timestamps(temp_wav)
            claude_seg_result = ask_claude_for_segments_with_timestamps(timestamped_segments, audio_duration)
            transcript = " ".join([s["text"] for s in timestamped_segments])
            filler_analysis = analyze_filler_words_from_text(transcript, audio_duration)
            pacing_analysis = analyze_pacing(transcript, audio_duration)
            final_transcript = transcript
            grammar_data = analyze_grammar_with_claude_sync(final_transcript)
            grammar_results = grammar_data.get("analysis", [])
            grammar_inline = grammar_data.get("inline", "")
            pdf_path = generate_pdf(transcript)
            pdf_key = f"uploads/pdf/{os.path.basename(pdf_path)}"
            with open(pdf_path, "rb") as f:
                pdf_url = upload_to_s3(f, pdf_key)
            os.remove(temp_original)
            os.remove(temp_wav)
            os.remove(pdf_path)

        # -------------------- LOCAL --------------------
        else:
            local_audio_dir = os.path.join(settings.MEDIA_ROOT, "audio")
            os.makedirs(local_audio_dir, exist_ok=True)
            temp_original_path = os.path.join(local_audio_dir, f"temp_{int(time.time())}.{file_format}")
            with open(temp_original_path, "wb") as f:
                for chunk in audio_file.chunks():
                    f.write(chunk)
            local_audio_path = os.path.join(local_audio_dir, f"converted_{int(time.time())}.wav")
            sound = AudioSegment.from_file(temp_original_path, format=file_format)
            sound = sound.set_channels(1).set_frame_rate(16000)
            sound.export(local_audio_path, format="wav")
            audio_duration = len(sound) / 1000.0
            timestamped_segments = transcribe_audio_with_timestamps(local_audio_path)
            filler_analysis = analyze_filler_words_from_text(transcript, audio_duration)
            claude_seg_result = ask_claude_for_segments_with_timestamps(timestamped_segments, audio_duration)
            transcript = " ".join([s["text"] for s in timestamped_segments])
            pacing_analysis = analyze_pacing(transcript, audio_duration)
            final_transcript = transcript
            grammar_data = analyze_grammar_with_claude_sync(final_transcript)
            grammar_results = grammar_data.get("analysis", [])
            grammar_inline = grammar_data.get("inline", "")
            pdf_path = generate_pdf(transcript)
            local_pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
            os.makedirs(local_pdf_dir, exist_ok=True)
            final_pdf_path = os.path.join(local_pdf_dir, os.path.basename(pdf_path))
            os.rename(pdf_path, final_pdf_path)
            s3_audio_url = settings.MEDIA_URL + f"audio/{os.path.basename(local_audio_path)}"
            pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(final_pdf_path)}"

    # -------------------- VIDEO UPLOAD --------------------
    if request.method == "POST" and request.FILES.get("video_file"):
        video_file = request.FILES["video_file"]
        video_format = video_file.name.split('.')[-1].lower()
        if USE_S3:
            s3_video_key = f"uploads/video/{video_file.name}"
            s3_video_url = upload_to_s3(video_file, s3_video_key)
            temp_video = tempfile.NamedTemporaryFile(delete=False, suffix=f".{video_format}").name
            download_from_s3(s3_video_key, temp_video)
            clip = VideoFileClip(temp_video)
            temp_audio = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
            clip.audio.write_audiofile(temp_audio, codec="pcm_s16le")
            clip.close()
            sound = AudioSegment.from_file(temp_audio)
            sound = sound.set_channels(1).set_frame_rate(16000)
            sound.export(temp_audio, format="wav")
            audio_duration = len(sound) / 1000.0
            timestamped_segments = transcribe_audio_with_timestamps(temp_audio)
            claude_seg_result = ask_claude_for_segments_with_timestamps(timestamped_segments, audio_duration)
            video_transcript = " ".join([s["text"] for s in timestamped_segments])
            final_transcript = video_transcript
            filler_analysis = analyze_filler_words_from_text(video_transcript, audio_duration)
            hedging_analysis = analyze_hedging_words(video_transcript, audio_duration)
            conciseness_data = analyze_conciseness(video_transcript, audio_duration)
            pacing_analysis = analyze_pacing(video_transcript, audio_duration)
            grammar_data = analyze_grammar_with_claude_sync(final_transcript)
            grammar_results = grammar_data.get("analysis", [])
            grammar_inline = grammar_data.get("inline", "")
            pdf_path = generate_pdf(video_transcript)
            pdf_key = f"uploads/pdf/{os.path.basename(pdf_path)}"
            with open(pdf_path, "rb") as f:
                pdf_url = upload_to_s3(f, pdf_key)
            os.remove(temp_video)
            os.remove(temp_audio)
            os.remove(pdf_path)

        else:
            vid_dir = os.path.join(settings.MEDIA_ROOT, "video")
            os.makedirs(vid_dir, exist_ok=True)
            local_video_path = os.path.join(vid_dir, video_file.name)
            with open(local_video_path, "wb") as f:
                for chunk in video_file.chunks():
                    f.write(chunk)
            clip = VideoFileClip(local_video_path)
            temp_wav = local_video_path.replace(f".{video_format}", ".wav")
            clip.audio.write_audiofile(temp_wav, codec="pcm_s16le")
            clip.close()
            sound = AudioSegment.from_file(temp_wav)
            sound = sound.set_channels(1).set_frame_rate(16000)
            sound.export(temp_wav, format="wav")
            audio_duration = len(sound) / 1000.0
            timestamped_segments = transcribe_audio_with_timestamps(temp_wav)
            claude_seg_result = ask_claude_for_segments_with_timestamps(timestamped_segments, audio_duration)
            video_transcript = " ".join([s["text"] for s in timestamped_segments])
            filler_analysis = analyze_filler_words_from_text(video_transcript, audio_duration)
            hedging_analysis = analyze_hedging_words(video_transcript, audio_duration)
            conciseness_data = analyze_conciseness(video_transcript, audio_duration)
            # filler_analysis = analyze_filler_words(video_transcript, audio_duration)
            final_transcript = video_transcript
            pacing_analysis = analyze_pacing(video_transcript, audio_duration)
            grammar_data = analyze_grammar_with_claude_sync(final_transcript)
            grammar_results = grammar_data.get("analysis", [])
            grammar_inline = grammar_data.get("inline", "")
            pdf_path = generate_pdf(video_transcript)
            pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
            os.makedirs(pdf_dir, exist_ok=True)
            final_pdf_path = os.path.join(pdf_dir, os.path.basename(pdf_path))
            os.rename(pdf_path, final_pdf_path)
            pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(final_pdf_path)}"

    # -------------------- FINAL SEGMENTS & DATABASE --------------------
    raw_segments = claude_seg_result["segments"] if 'claude_seg_result' in locals() else []
    pacing_segments = adjust_segments_to_audio(raw_segments, audio_duration)
    final_transcript = transcript or video_transcript
    hedging_analysis = analyze_hedging_words(final_transcript, audio_duration)
    conciseness_data = analyze_conciseness(final_transcript, audio_duration)

    
    pace_segments = []

    for seg in pacing_segments:
        words = len(seg["text"].split())
        duration = max(seg["end_s"] - seg["start_s"], 0.1)
        wpm = (words / duration) * 60
        pace_label = classify_pace_segment(wpm)

        pace_segments.append({
            "label": pace_label,
            "start_s": seg["start_s"],
            "end_s": seg["end_s"],
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"],
            "wpm": round(wpm)
        })
    pacing_segments = mark_pause_segments(pacing_segments)
    # Build filler word list from detected frequencies
    filler_list = list(filler_analysis.get("filler_frequency", {}).keys())

    for seg in pacing_segments:
        words = len(seg["text"].split())
        duration = seg["end_s"] - seg["start_s"]
        wpm = (words / duration) * 60 if duration else 0

        seg["label"] = classify_pace_segment(wpm)

        # ✅ highlight filler words
        seg["text"] = highlight_filler_words(
            seg["text"],
            FILLER_WORDS,
            seg["start_s"],
            seg["end_s"]
        )

        # ✅ highlight hedging words (NEW)
        seg["text"] = highlight_hedging_words(
            seg["text"],
            HEDGING_WORDS,
            seg["start_s"],
            seg["end_s"]
        )


    # Apply filler-word highlighting using CLAUDE filler list
    

    pacing_analysis = {"wpm": claude_seg_result.get("wpm", 0), **pacing_analysis}

    if request.user.is_authenticated and (transcript or video_transcript):
        Recording.objects.create(
            user=request.user,
            title=f"Recording - {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
            audio_url=s3_audio_url or s3_video_url,
            pdf_url=pdf_url,
            transcript=transcript or video_transcript,
            filler_data=filler_analysis,
            pacing_data=pacing_analysis,
            pacing_segments={
                "segments": pacing_segments,
                "pace_segments": pace_segments
            },
            grammar_data=grammar_data.get("analysis", []),
            duration=audio_duration,
            hedging_data=hedging_analysis,
            conciseness_data=conciseness_data
        )


    return render(request, "index3.html", {
        "transcript": transcript,
        "video_transcript": video_transcript,
        "s3_audio_url": s3_audio_url or s3_video_url,
        "pdf_url": pdf_url,
        "filler_analysis": filler_analysis,
        "pacing_analysis": pacing_analysis,
        "pacing_segments": pacing_segments,
        "grammar_results": grammar_results,
        "grammar_inline": grammar_inline,
        "grammar_data": grammar_data,
    })
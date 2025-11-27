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

import asyncio
import json
from typing import Dict, List


# Add these imports to your existing code
# pip install anthropic
try:
    import anthropic
    CLAUDE_AVAILABLE = True
except ImportError:
    CLAUDE_AVAILABLE = False

SUPPORTED_AUDIO_FORMATS = ['mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac', 'wma', 'webm', 'opus']
SUPPORTED_VIDEO_FORMATS = ['mp4', 'avi', 'mov', 'mkv', 'flv', 'wmv', 'webm']

# PRACTICE TAB (default dashboard)
@login_required(login_url='login')
def practice_view(request):
    return render(request, 'index3.html')


# RECORDING TAB (shows list)
@login_required
def recording_view(request):
    recordings = Recording.objects.filter(user=request.user).order_by('-created_at')
    paginator = Paginator(recordings, 10)  # 5 per page
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
    
    context = {
        'rec': rec,
        'grammar_inline': grammar_inline,
        'grammar_data': grammar_data,
        'grammar_results': grammar_results,
        'conciseness': conciseness,
        'speaking_tips': speaking_tips,  # NEW: Add tips to context
    }
    
    return render(request, 'recording_detail.html', context)

    def generate_inline_html_from_stored_data(original_text: str, grammar_results: list) -> str:
        """
        Generate inline HTML from previously stored grammar results.
        Used when displaying existing recordings.
        """
        if not grammar_results or not original_text:
            return html.escape(original_text) if original_text else ""
        
        result = original_text
        positioned_errors = []
        
        # Convert stored grammar data to positioned errors
        for error in grammar_results:
            if isinstance(error, dict):
                # Handle both Claude format and language_tool format
                original = error.get('original', '') or error.get('context', '')
                
                # Extract correction from various possible formats
                correction = error.get('correction', '')
                if not correction and error.get('suggestions'):
                    suggestions = error.get('suggestions')
                    if isinstance(suggestions, list) and suggestions:
                        correction = suggestions[0]
                
                error_type = error.get('type', 'grammar')
                explanation = error.get('explanation', '') or error.get('issue', '')
                
                # Only process if we have both original and correction
                if original and correction and original in result:
                    pos = result.find(original)
                    if pos != -1:
                        positioned_errors.append({
                            'pos': pos,
                            'original': original,
                            'correction': correction,
                            'type': error_type,
                            'explanation': explanation
                        })
        
        # Sort by position (reverse order to avoid offset issues)
        positioned_errors.sort(key=lambda x: x['pos'], reverse=True)
        
        # Apply replacements from end to start
        for error in positioned_errors:
            original_text_part = error['original']
            correction = error['correction']
            error_type = error['type']
            explanation = error['explanation']
            pos = error['pos']
            
            # Create the replacement HTML
            replacement = (
                f"<span title='{error_type}: {explanation}'>"
                f"<del style='color:#d93025;text-decoration:line-through;'>{html.escape(original_text_part)}</del>"
                f"<ins style='color:#1a8917;font-weight:bold;'> {html.escape(correction)}</ins>"
                f"</span>"
            )
            
            # Replace at specific position
            result = result[:pos] + replacement + result[pos + len(original_text_part):]
        
    return result


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


# -------------------------------------------------------------------
# ✅ SPEECH RECOGNITION
# -------------------------------------------------------------------
def transcribe_audio(local_wav_path):
    """Transcribe WAV audio into text using Google Speech Recognition (handles long audio)."""
    recognizer = sr.Recognizer()
    transcript_parts = []

    with sr.AudioFile(local_wav_path) as source:
        audio_duration = source.DURATION if hasattr(source, 'DURATION') else None
        print(f"⏱️ Transcribing audio... Duration unknown" if not audio_duration else f"⏱️ Audio duration: {audio_duration}s")

        # Process in small chunks (~30 seconds each)
        chunk_duration = 30  # seconds
        try:
            total_duration = int(AudioSegment.from_wav(local_wav_path).duration_seconds)
            for start_time in range(0, total_duration, chunk_duration):
                with sr.AudioFile(local_wav_path) as sub_source:
                    sub_source.DURATION = total_duration
                    recognizer.adjust_for_ambient_noise(sub_source, duration=0.2)
                    audio_data = recognizer.record(sub_source, offset=start_time, duration=chunk_duration)

                try:
                    text = recognizer.recognize_google(audio_data)
                    transcript_parts.append(text)
                    print(f"✅ Transcribed chunk {start_time // chunk_duration + 1}")
                except sr.UnknownValueError:
                    transcript_parts.append("[Unrecognized segment]")
                except sr.RequestError as e:
                    print(f"⚠️ Google API error in chunk starting at {start_time}s: {e}")
                    transcript_parts.append("[API error segment]")
        except Exception as e:
            return f"⚠️ Speech recognition failed: {e}"

    full_transcript = " ".join(transcript_parts).strip()
    return full_transcript if full_transcript else "⚠️ No recognizable speech found."


# -------------------------------------------------------------------
# ✅ ANALYSIS HELPERS
# -------------------------------------------------------------------
def analyze_filler_words(transcript, audio_duration):
    filler_words = ["the", "old", "um", "uh", "like", "you know", "basically",
                    "actually", "literally", "so", "well", "hmm"]
    text = transcript.lower()
    found_fillers = []
    for word in filler_words:
        matches = re.findall(rf'\b{re.escape(word)}\b', text)
        found_fillers.extend(matches)

    filler_count = len(found_fillers)
    filler_freq = dict(Counter(found_fillers))
    minutes = audio_duration / 60 if audio_duration else 1
    fillers_per_minute = round(filler_count / minutes, 2)

    return {
        "total_fillers": filler_count,
        "filler_frequency": filler_freq,
        "fillers_per_minute": fillers_per_minute
    }


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
        
        prompt = f"""Analyze this SPOKEN LANGUAGE transcript for grammar errors that would actually be noticeable when speaking.

IMPORTANT RULES:
- DO NOT flag capitalization errors (people don't speak capital letters)
- DO NOT flag punctuation issues
- ONLY flag errors that affect spoken grammar understanding
- Focus on errors a listener would notice

Check ONLY these spoken grammar issues:
1. Missing articles (a, an, the) - Example: "I am apple" → "I am an apple"
2. Subject-verb agreement - Example: "She go" → "She goes"
3. Incorrect verb tenses - Example: "Yesterday I go" → "Yesterday I went"
4. Plural/singular errors - Example: "Two book" → "Two books"
5. Pronoun errors ONLY if clearly wrong - Example: "Him like it" → "He likes it"

DO NOT flag:
- Possessive vs subject pronouns if both make sense (your/you, etc.)
- Minor stylistic preferences
- Capitalization
- Punctuation

Transcript: "{text}"

Return ONLY valid JSON (no markdown):
{{
  "corrected_text": "The corrected version",
  "errors": [
    {{
      "original": "exact error text",
      "correction": "corrected version",
      "type": "article|verb|tense|plural|pronoun",
      "explanation": "why this is wrong in spoken language"
    }}
  ]
}}"""

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
        inline_html = generate_inline_html_from_claude(text, result.get('errors', []))
        
        return {
            "corrected": result.get('corrected_text', text),
            "inline": inline_html,
            "issues": len(result.get('errors', [])),
            "analysis": result.get('errors', [])
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

def generate_inline_html_from_claude(original_text: str, errors: List[Dict]) -> str:
    """Generate HTML with corrections"""
    if not errors:
        return html.escape(original_text)
    
    result = original_text
    positioned_errors = []
    
    for error in errors:
        original = error.get('original', '')
        if original and original in result:
            pos = result.find(original)
            if pos != -1:
                positioned_errors.append({
                    'pos': pos,
                    'original': original,
                    'correction': error.get('correction', ''),
                    'type': error.get('type', 'grammar'),
                    'explanation': error.get('explanation', '')
                })
    
    positioned_errors.sort(key=lambda x: x['pos'], reverse=True)
    
    for error in positioned_errors:
        replacement = (
            f"<span title='{error['type']}: {error['explanation']}'>"
            f"<del style='color:#d93025;text-decoration:line-through;'>{html.escape(error['original'])}</del>"
            f"<ins style='color:#1a8917;font-weight:bold;'> {html.escape(error['correction'])}</ins>"
            f"</span>"
        )
        result = result[:error['pos']] + replacement + result[error['pos'] + len(error['original']):]
    
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
    grammar_results = []
    grammar_inline = ""
    grammar_data = {}  # NEW: Store full grammar analysis

    USE_S3 = bool(getattr(settings, "AWS_STORAGE_BUCKET_NAME", ""))

    # ============================================================
    # APPLY MANUAL CORRECTIONS
    # ============================================================
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

    # ============================================================
    # AUTO GRAMMAR CORRECTION
    # ============================================================
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

    # ============================================================
    # AUDIO UPLOAD
    # ============================================================
    audio_file = None
    file_format = None
    audio_processing_enabled = False   # NEW FLAG

    if request.method == "POST" and request.FILES.get("audio_file"):
        audio_file = request.FILES["audio_file"]
        file_format = audio_file.name.split('.')[-1].lower()
        audio_processing_enabled = True   # Audio exists → enable block

    # If no audio uploaded, just SKIP audio processing
    if audio_processing_enabled:
        # Validate audio format
        if file_format not in SUPPORTED_AUDIO_FORMATS:
            return render(request, "index3.html", {
                "error": f"Unsupported audio format: .{file_format}. Supported formats: {', '.join(SUPPORTED_AUDIO_FORMATS)}"
            })

        if not USE_S3:
            # LOCAL STORAGE PATH
            local_audio_dir = os.path.join(settings.MEDIA_ROOT, "audio")
            os.makedirs(local_audio_dir, exist_ok=True)
            
            # Save with original format first
            temp_original_path = os.path.join(local_audio_dir, f"temp_{int(time.time())}.{file_format}")
            with open(temp_original_path, "wb") as f:
                for chunk in audio_file.chunks():
                    f.write(chunk)

            # Convert to WAV for transcription
            local_audio_path = os.path.join(local_audio_dir, f"converted_{int(time.time())}.wav")
            try:
                sound = AudioSegment.from_file(temp_original_path, format=file_format)
                sound = sound.set_channels(1)
                sound = sound.set_frame_rate(16000)
                sound.export(local_audio_path, format="wav")
                audio_duration = len(sound) / 1000.0
                os.remove(temp_original_path)
            except Exception as e:
                if os.path.exists(temp_original_path):
                    os.remove(temp_original_path)
                return render(request, "index3.html", {
                    "error": f"Could not process audio file: {str(e)}. Make sure ffmpeg is installed."
                })

            # Transcribe
            transcript = transcribe_audio(local_audio_path)
            filler_analysis = analyze_filler_words(transcript, audio_duration)
            pacing_analysis = analyze_pacing(transcript, audio_duration)
            grammar_results = analyze_grammar(transcript)

            # Generate PDF
            pdf_path = generate_pdf(
                transcript,
                filler_analysis=filler_analysis,
                pacing_analysis=pacing_analysis,
                grammar_inline=grammar_inline
            )
            local_pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
            os.makedirs(local_pdf_dir, exist_ok=True)
            local_pdf_path = os.path.join(local_pdf_dir, os.path.basename(pdf_path))
            os.rename(pdf_path, local_pdf_path)

            s3_audio_url = settings.MEDIA_URL + f"audio/{os.path.basename(local_audio_path)}"
            pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(local_pdf_path)}"

        else:
            # S3 STORAGE PATH
            s3_audio_key = f"uploads/audio/{int(time.time())}_{audio_file.name}"
            s3_audio_url = upload_to_s3(audio_file, s3_audio_key)

            temp_original = tempfile.NamedTemporaryFile(delete=False, suffix=f".{file_format}").name
            temp_wav_path = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
            
            try:
                download_from_s3(s3_audio_key, temp_original)
                sound = AudioSegment.from_file(temp_original, format=file_format)
                sound = sound.set_channels(1)
                sound = sound.set_frame_rate(16000)
                sound.export(temp_wav_path, format="wav")
                audio_duration = len(sound) / 1000.0
            except Exception as e:
                os.remove(temp_original)
                os.remove(temp_wav_path)
                return render(request, "index3.html", {
                    "error": f"Could not process audio: {str(e)}"
                })

            transcript = transcribe_audio(temp_wav_path)
            filler_analysis = analyze_filler_words(transcript, audio_duration)
            pacing_analysis = analyze_pacing(transcript, audio_duration)
            grammar_results = analyze_grammar(transcript)

            pdf_path = generate_pdf(
                transcript,
                filler_analysis=filler_analysis,
                pacing_analysis=pacing_analysis,
                grammar_inline=grammar_inline
            )

            pdf_key = f"uploads/pdf/{os.path.basename(pdf_path)}"
            with open(pdf_path, "rb") as pdf_file:
                pdf_url = upload_to_s3(pdf_file, pdf_key)

            os.remove(temp_original)
            os.remove(temp_wav_path)
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    # ============================================================
    # VIDEO UPLOAD
    # ============================================================
    if request.method == "POST" and request.FILES.get("video_file"):
        video_file = request.FILES["video_file"]
        video_format = video_file.name.split('.')[-1].lower()

        # ---------- LOCAL ----------
        if not USE_S3:
            vid_dir = os.path.join(settings.MEDIA_ROOT, "video")
            os.makedirs(vid_dir, exist_ok=True)

            local_video_path = os.path.join(vid_dir, video_file.name)
            with open(local_video_path, "wb") as f:
                for chunk in video_file.chunks():
                    f.write(chunk)

            try:
                clip = VideoFileClip(local_video_path)
                local_audio_path = local_video_path.replace(f".{video_format}", ".wav")
                clip.audio.write_audiofile(local_audio_path, codec="pcm_s16le")
                clip.close()
            except Exception:
                return render(request, "index3.html", {"error": "Audio extraction failed."})

            sound = AudioSegment.from_file(local_audio_path)
            sound = sound.set_channels(1).set_frame_rate(16000)
            sound.export(local_audio_path, format="wav")
            audio_duration = len(sound) / 1000

            video_transcript = transcribe_audio(local_audio_path)

            filler_analysis = analyze_filler_words(video_transcript, audio_duration)
            pacing_analysis = analyze_pacing(video_transcript, audio_duration)
            grammar_results = analyze_grammar(video_transcript)

            pdf_path = generate_pdf(video_transcript)
            pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
            os.makedirs(pdf_dir, exist_ok=True)
            final_pdf_path = os.path.join(pdf_dir, os.path.basename(pdf_path))
            os.rename(pdf_path, final_pdf_path)
            pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(final_pdf_path)}"

        # ---------- S3 ----------
        else:
            s3_video_key = f"uploads/video/{video_file.name}"
            upload_to_s3(video_file, s3_video_key)

            temp_video = tempfile.NamedTemporaryFile(delete=False, suffix=f".{video_format}").name
            download_from_s3(s3_video_key, temp_video)

            try:
                clip = VideoFileClip(temp_video)
                temp_audio = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
                clip.audio.write_audiofile(temp_audio, codec="pcm_s16le")
                clip.close()
            except Exception:
                return render(request, "index3.html", {"error": "Audio extraction failed."})

            sound = AudioSegment.from_file(temp_audio)
            sound = sound.set_channels(1).set_frame_rate(16000)
            sound.export(temp_audio, format="wav")
            audio_duration = len(sound) / 1000

            video_transcript = transcribe_audio(temp_audio)

            filler_analysis = analyze_filler_words(video_transcript, audio_duration)
            pacing_analysis = analyze_pacing(video_transcript, audio_duration)
            grammar_results = analyze_grammar(video_transcript)

            pdf_path = generate_pdf(video_transcript)
            pdf_key = f"uploads/pdf/{os.path.basename(pdf_path)}"
            with open(pdf_path, "rb") as f:
                pdf_url = upload_to_s3(f, pdf_key)

            os.remove(temp_video)
            os.remove(temp_audio)
            os.remove(pdf_path)

    # ============================================================
    # INLINE GRAMMAR HIGHLIGHT
    # ============================================================
    if transcript:
        grammar_data = analyze_grammar_with_claude_sync(transcript)
        grammar_inline = grammar_data['inline']
        grammar_results = grammar_data['analysis']
    
    elif video_transcript:
        grammar_data = analyze_grammar_with_claude_sync(video_transcript)
        grammar_inline = grammar_data['inline']
        grammar_results = grammar_data['analysis']

    # ============================================================
    # SAVE TO DATABASE
    # ============================================================
    if request.user.is_authenticated and (transcript or video_transcript):
        Recording.objects.create(
            user=request.user,
            title=f"Recording - {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
            audio_url=s3_audio_url or s3_video_url,
            pdf_url=pdf_url,
            transcript=transcript or video_transcript,
            filler_data=filler_analysis,
            pacing_data=pacing_analysis,
            grammar_data=grammar_results,  # Now includes Claude's detailed analysis
            duration=pacing_analysis.get("wpm", 0),
        )

    # ============================================================
    # RETURN RESPONSE
    # ============================================================
    return render(request, "index3.html", {
        "transcript": transcript,
        "video_transcript": video_transcript,
        "s3_audio_url": s3_audio_url,
        "pdf_url": pdf_url,
        "filler_analysis": filler_analysis,
        "pacing_analysis": pacing_analysis,
        "grammar_results": grammar_results,
        "grammar_inline": grammar_inline,
        "grammar_data": grammar_data,  # ADD THIS LINE
    })

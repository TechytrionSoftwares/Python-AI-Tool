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


from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.shortcuts import redirect
from django.core.paginator import Paginator

# PRACTICE TAB (default dashboard)
@login_required(login_url='login')
def practice_view(request):
    return render(request, 'index3.html')


# RECORDING TAB (shows list)
@login_required
def recording_view(request):
    recordings = Recording.objects.filter(user=request.user).order_by('-created_at')
    paginator = Paginator(recordings, 5)  # 5 per page
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    return render(request, 'recording.html', {'recordings': page_obj})

@login_required
def delete_recordings(request):
    if request.method == "POST":
        selected_ids = request.POST.getlist('selected_ids')
        Recording.objects.filter(id__in=selected_ids, user=request.user).delete()
    return redirect('recording')   
    
@login_required
def recording_detail(request, rec_id):
    from .models import Recording
    rec = Recording.objects.get(id=rec_id, user=request.user)
    return render(request, "recording_detail.html", {"rec": rec})


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


@login_required(login_url='login')
# -------------------------------------------------------------------
# ✅ MAIN VIEW — HANDLES AUDIO + VIDEO
# -------------------------------------------------------------------
def speech_tx(request):
    transcript = ""
    video_transcript = ""
    pdf_url = ""
    s3_audio_url = ""
    filler_analysis = {}
    pacing_analysis = {}
    grammar_results = []
    grammar_inline = ""

    USE_S3 = hasattr(settings, "AWS_STORAGE_BUCKET_NAME") and settings.AWS_STORAGE_BUCKET_NAME

    # ---------------- APPLY CORRECTIONS ----------------
    if request.method == "POST" and request.POST.get("action") == "apply_corrections":
        corrected_text = request.POST.get("corrected_text", "")
        pdf_path = generate_pdf(corrected_text)

        if USE_S3:
            pdf_key = f"uploads/pdf/corrected_{int(time.time())}.pdf"
            with open(pdf_path, "rb") as pdf_file:
                pdf_url = upload_to_s3(pdf_file, pdf_key)
        else:
            local_pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
            os.makedirs(local_pdf_dir, exist_ok=True)
            local_pdf_path = os.path.join(local_pdf_dir, f"corrected_{int(time.time())}.pdf")
            os.rename(pdf_path, local_pdf_path)
            pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(local_pdf_path)}"

        return render(request, "index3.html", {
            "transcript": corrected_text,
            "pdf_url": pdf_url,
            "message": "✅ Grammar corrections applied successfully!"
        })

    # ---------------- AUTO GRAMMAR FIX ----------------
    if request.method == "POST" and request.POST.get("action") == "auto_correct_grammar":
        original_text = request.POST.get("original_text", "")
        corrected_text = apply_grammar_corrections(original_text)

        pdf_path = generate_pdf(corrected_text)
        local_pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
        os.makedirs(local_pdf_dir, exist_ok=True)
        local_pdf_path = os.path.join(local_pdf_dir, f"auto_corrected_{int(time.time())}.pdf")
        os.rename(pdf_path, local_pdf_path)
        pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(local_pdf_path)}"

        return render(request, "index3.html", {
            "transcript": corrected_text,
            "pdf_url": pdf_url,
            "message": "✅ Grammar corrections applied automatically!"
        })

    # ---------------- AUDIO FILE UPLOAD ----------------
    if request.method == "POST" and request.FILES.get("audio_file"):
        audio_file = request.FILES["audio_file"]
        file_format = audio_file.name.split('.')[-1].lower()

        if not USE_S3:
            local_audio_dir = os.path.join(settings.MEDIA_ROOT, "audio")
            os.makedirs(local_audio_dir, exist_ok=True)
            local_audio_path = os.path.join(local_audio_dir, audio_file.name)

            with open(local_audio_path, "wb") as f:
                for chunk in audio_file.chunks():
                    f.write(chunk)

            sound = AudioSegment.from_file(local_audio_path, format=file_format)
            sound.export(local_audio_path, format="wav")
            audio_duration = len(sound) / 1000.0

            transcript = transcribe_audio(local_audio_path)
            filler_analysis = analyze_filler_words(transcript, audio_duration)
            pacing_analysis = analyze_pacing(transcript, audio_duration)
            grammar_results = analyze_grammar(transcript)

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
            s3_audio_key = f"uploads/audio/{audio_file.name}"
            s3_audio_url = upload_to_s3(audio_file, s3_audio_key)

            temp_wav_path = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
            download_from_s3(s3_audio_key, temp_wav_path)

            sound = AudioSegment.from_file(temp_wav_path, format=file_format)
            sound.export(temp_wav_path, format="wav")
            audio_duration = len(sound) / 1000.0

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

            os.remove(temp_wav_path)
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    # ---------------- VIDEO FILE UPLOAD ----------------
    if request.method == "POST" and request.FILES.get("video_file"):
        video_file = request.FILES["video_file"]
        video_format = video_file.name.split('.')[-1].lower()

        if not USE_S3:
            local_video_dir = os.path.join(settings.MEDIA_ROOT, "video")
            os.makedirs(local_video_dir, exist_ok=True)
            local_video_path = os.path.join(local_video_dir, video_file.name)

            with open(local_video_path, "wb") as f:
                for chunk in video_file.chunks():
                    f.write(chunk)

            try:
                clip = VideoFileClip(local_video_path)
                local_audio_path = os.path.splitext(local_video_path)[0] + ".wav"
                clip.audio.write_audiofile(local_audio_path, codec="pcm_s16le")
                clip.close()
            except Exception as e:
                print(f"❌ Video extract error: {e}")
                return render(request, "index3.html", {"error": "Could not extract audio from video."})

            try:
                sound = AudioSegment.from_file(local_audio_path, format="wav")
                sound = sound.set_channels(1)
                sound = sound.set_frame_rate(16000)
                sound.export(local_audio_path, format="wav")
                audio_duration = len(sound) / 1000.0
            except Exception as e:
                print(f"❌ Audio processing error: {e}")
                return render(request, "index3.html", {"error": "Audio processing failed."})

            video_transcript = transcribe_audio(local_audio_path)
            filler_analysis = analyze_filler_words(video_transcript, audio_duration)
            pacing_analysis = analyze_pacing(video_transcript, audio_duration)
            grammar_results = analyze_grammar(video_transcript)

            pdf_path = generate_pdf(video_transcript)
            local_pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdf")
            os.makedirs(local_pdf_dir, exist_ok=True)
            local_pdf_path = os.path.join(local_pdf_dir, os.path.basename(pdf_path))
            os.rename(pdf_path, local_pdf_path)
            pdf_url = settings.MEDIA_URL + f"pdf/{os.path.basename(local_pdf_path)}"

        else:
            s3_video_key = f"uploads/video/{video_file.name}"
            upload_to_s3(video_file, s3_video_key)

            temp_video_path = tempfile.NamedTemporaryFile(delete=False, suffix=f".{video_format}").name
            download_from_s3(s3_video_key, temp_video_path)

            try:
                clip = VideoFileClip(temp_video_path)
                temp_wav_path = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
                clip.audio.write_audiofile(temp_wav_path, codec="pcm_s16le")
                clip.close()
            except Exception as e:
                print(f"❌ Video extract error: {e}")
                return render(request, "index3.html", {"error": "Could not extract audio from video."})

            sound = AudioSegment.from_file(temp_wav_path, format="wav")
            sound = sound.set_channels(1)
            sound = sound.set_frame_rate(16000)
            sound.export(temp_wav_path, format="wav")
            audio_duration = len(sound) / 1000.0

            video_transcript = transcribe_audio(temp_wav_path)
            filler_analysis = analyze_filler_words(video_transcript, audio_duration)
            pacing_analysis = analyze_pacing(video_transcript, audio_duration)
            grammar_results = analyze_grammar(video_transcript)

            pdf_path = generate_pdf(video_transcript)
            pdf_key = f"uploads/pdf/{os.path.basename(pdf_path)}"
            with open(pdf_path, "rb") as pdf_file:
                pdf_url = upload_to_s3(pdf_file, pdf_key)

            os.remove(temp_video_path)
            os.remove(temp_wav_path)
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    # ---------------- GRAMMAR HIGHLIGHT MODE ----------------
    grammar_inline = ""
    if transcript:
        tool = language_tool_python.LanguageTool('en-US')
        matches = tool.check(transcript)
        corrected_text = transcript
        offset = 0

        for match in matches:
            if match.replacements:
                start = match.offset + offset
                end = match.errorLength + start
                incorrect = corrected_text[start:end]
                suggestion = match.replacements[0]

                replacement_html = (
                    f'<span style="color:red;text-decoration:line-through;">{incorrect}</span>'
                    f'<span style="color:green;font-weight:bold;">{suggestion}</span>'
                )

                corrected_text = corrected_text[:start] + replacement_html + corrected_text[end:]
                offset += len(replacement_html) - len(incorrect)

        grammar_inline = corrected_text
        # ---------------- SAVE RECORDING TO DATABASE ----------------
    if request.user.is_authenticated and (transcript or video_transcript):
        try:
            # Determine whether it’s audio or video
            file_url = None
            if s3_audio_url:
                file_url = s3_audio_url
            elif 's3_video_key' in locals():
                # when using S3 for videos
                file_url = f"https://{settings.AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com/{s3_video_key}"
            elif 'local_audio_path' in locals():
                file_url = settings.MEDIA_URL + f"audio/{os.path.basename(local_audio_path)}"
            elif 'local_video_path' in locals():
                file_url = settings.MEDIA_URL + f"video/{os.path.basename(local_video_path)}"

            Recording.objects.create(
                user=request.user,
                title=f"Recording - {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
                audio_url=file_url,  # ✅ will be video or audio URL
                pdf_url=pdf_url,
                transcript=transcript or video_transcript,
                filler_data=filler_analysis or {},
                pacing_data=pacing_analysis or {},
                grammar_data=grammar_results or {},
                duration=pacing_analysis.get("wpm", 0),
            )
            print("✅ Recording saved successfully")
        except Exception as e:
            print(f"⚠️ Failed to save recording: {e}")


    # ---------------- RENDER RESPONSE ----------------
    return render(request, "index3.html", {
        "transcript": transcript,
        "video_transcript": video_transcript,
        "s3_audio_url": s3_audio_url,
        "pdf_url": pdf_url,
        "filler_analysis": filler_analysis,
        "pacing_analysis": pacing_analysis,
        "grammar_results": grammar_results,
        "grammar_inline": grammar_inline,
    })

from django.shortcuts import render
from django.conf import settings
import boto3, os, tempfile
from pydub import AudioSegment
import speech_recognition as sr
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.units import inch
from reportlab.lib import colors
import tempfile


import re
from collections import Counter
# ---------- Helper Functions ---------- #


# File Upload the S3 bucket in aws start code 3/11/2025
def upload_to_s3(file_obj, key):
    """Upload a file object to AWS S3 and return its URL."""
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME
    )
    s3.upload_fileobj(file_obj, settings.AWS_STORAGE_BUCKET_NAME, key)
    return f"https://{settings.AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com/{key}"

# File Upload the S3 bucket in aws end code 3/11/2025

#  Download pdf the File S3 bucket in aws start code 3/11/2025
def download_from_s3(key, local_path):
    """Download a file from S3 to a local path."""
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME
    )
    s3.download_file(settings.AWS_STORAGE_BUCKET_NAME, key, local_path)
#  Download pdf the File S3 bucket in aws end code 3/11/2025


#  generate_pdf the Function aws start code 3/11/2025
def generate_pdf(transcript_text):
    """Generate a well-formatted PDF file from transcript text and return local path."""
    temp_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    doc = SimpleDocTemplate(temp_pdf.name, pagesize=letter,
                            rightMargin=50, leftMargin=50, topMargin=50, bottomMargin=50)

    styles = getSampleStyleSheet()
    story = []

    # Add title
    title_style = styles["Heading2"]
    title_style.textColor = colors.HexColor("#1A237E")
    story.append(Paragraph("Speech-to-Text Transcript", title_style))
    story.append(Spacer(1, 0.3 * inch))

    # Add main transcript text with word wrapping
    body_style = styles["Normal"]
    body_style.fontSize = 12
    body_style.leading = 18
    paragraphs = transcript_text.strip().split("\n")

    for para in paragraphs:
        story.append(Paragraph(para, body_style))
        story.append(Spacer(1, 0.2 * inch))

    # Build PDF
    doc.build(story)
    return temp_pdf.name

#  generate_pdf the end aws start code 3/11/2025

#  transcribe audio the Function aws start code 3/11/2025
def transcribe_audio(local_wav_path):
    """Transcribe a WAV audio file into text."""
    recognizer = sr.Recognizer()
    with sr.AudioFile(local_wav_path) as source:
        audio_data = recognizer.record(source)
        return recognizer.recognize_google(audio_data)

#  transcribe audio the Function aws END code 3/11/2025


#  analyze filler words audio the Function aws start code 3/11/2025
def analyze_filler_words(transcript, audio_duration):
    """
    Analyze transcript text to find filler words and calculate statistics.
    """
    filler_words = ["the", "old", "um", "uh", "like", "you know", "basically", "actually", "literally", "so", "well", "hmm"]
    
    # Normalize text
    text = transcript.lower()
    
    # Count occurrences of filler words
    found_fillers = []
    for word in filler_words:
        # Match standalone words or phrases (case-insensitive)
        matches = re.findall(rf'\b{re.escape(word)}\b', text)
        found_fillers.extend(matches)

    filler_count = len(found_fillers)
    filler_freq = dict(Counter(found_fillers))

    # Fillers per minute
    minutes = audio_duration / 60 if audio_duration else 1
    fillers_per_minute = round(filler_count / minutes, 2)

    return {
        "total_fillers": filler_count,
        "filler_frequency": filler_freq,
        "fillers_per_minute": fillers_per_minute
    }
#  analyze filler words audio the Function aws END code 3/11/2025


#  analyze_pacing words audio the Function aws start code 3/11/2025
def analyze_pacing(transcript, audio_duration):
    """
    Analyze the speech pacing by calculating Words Per Minute (WPM)
    and suggesting if it's too slow, too fast, or ideal.
    """
    if not transcript or audio_duration <= 0:
        return {"wpm": 0, "pace_feedback": "Insufficient data for pacing analysis."}

    # Count words in transcript
    word_count = len(transcript.split())
    minutes = audio_duration / 60.0
    wpm = round(word_count / minutes, 2)

    # Evaluate pacing
    if wpm < 125:
        pace_feedback = "🟡 You're speaking a bit slowly. Try increasing your pace slightly."
    elif wpm > 160:
        pace_feedback = "🔴 You're speaking quite fast. Slow down a bit for better clarity."
    else:
        pace_feedback = "🟢 Great! Your speaking pace is within the ideal range (125–160 WPM)."

    return {"wpm": wpm, "pace_feedback": pace_feedback}
#  analyze_pacing words audio the Function aws END code 3/11/2025


#  grammar_check words audio the Function aws start code 3/11/2025  ----------still Pending

import openai

def grammar_check(transcript):
    """
    Uses GPT to check grammar and clarity and suggest rephrased sentences.
    """
    if not transcript.strip():
        return []

    try:
        prompt = f"""
        Analyze the following text for grammar and clarity issues.
        For each sentence, return a JSON list with 'original' and 'suggestion'.

        Text:
        {transcript}
        """

        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "You are a helpful writing assistant."},
                      {"role": "user", "content": prompt}],
            temperature=0.3
        )

        import json
        text = response.choices[0].message["content"].strip()
        return json.loads(text)

    except Exception as e:
        print("Grammar check failed:", e)
        return [{"original": transcript, "suggestion": "Grammar check unavailable."}]


#  grammar_check words audio the Function aws end code 3/11/2025  ----------still Pending

def speech_tx(request):
    transcript = ""
    pdf_url = ""
    s3_audio_url = ""
    filler_analysis = {}
    pacing_analysis = {}
    grammar_results = []

    # Handle Apply Corrections button
    if request.method == "POST" and request.POST.get("action") == "apply_corrections":
        corrected_text = request.POST.get("corrected_text", "")
        pdf_path = generate_pdf(corrected_text)
        pdf_key = f"uploads/pdf/corrected_{int(time.time())}.pdf"
        with open(pdf_path, "rb") as pdf_file:
            pdf_url = upload_to_s3(pdf_file, pdf_key)
        return render(request, "index3.html", {
            "transcript": corrected_text,
            "pdf_url": pdf_url,
            "message": "✅ Grammar corrections applied successfully!"
        })

    if request.method == "POST" and request.FILES.get("audio_file"):
        audio_file = request.FILES["audio_file"]
        file_format = audio_file.name.split('.')[-1].lower()
        s3_audio_key = f"uploads/audio/{audio_file.name}"

        # Upload raw audio to S3
        s3_audio_url = upload_to_s3(audio_file, s3_audio_key)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp_wav:
            try:
                # Convert to WAV
                download_from_s3(s3_audio_key, temp_wav.name)
                sound = AudioSegment.from_file(temp_wav.name, format=file_format)
                sound.export(temp_wav.name, format="wav")
                audio_duration = len(sound) / 1000.0

                # Step 1: Transcribe
                transcript = transcribe_audio(temp_wav.name)

                # Step 2: Filler Word Analysis
                filler_analysis = analyze_filler_words(transcript, audio_duration)

                # Step 3: Pacing Analysis
                pacing_analysis = analyze_pacing(transcript, audio_duration)

                # Step 4: Grammar Check
                grammar_results = grammar_check(transcript)

                # Step 5: PDF generation
                pdf_path = generate_pdf(transcript)
                pdf_key = f"uploads/pdf/{os.path.basename(pdf_path)}"
                with open(pdf_path, "rb") as pdf_file:
                    pdf_url = upload_to_s3(pdf_file, pdf_key)

            finally:
                if os.path.exists(temp_wav.name):
                    os.remove(temp_wav.name)
                if pdf_path and os.path.exists(pdf_path):
                    os.remove(pdf_path)

    return render(request, "index3.html", {
        "transcript": transcript,
        "s3_audio_url": s3_audio_url,
        "pdf_url": pdf_url,
        "filler_analysis": filler_analysis,
        "pacing_analysis": pacing_analysis,
        "grammar_results": grammar_results
    })


def calculate_overall_score(filler_analysis, pacing_analysis, grammar_results):
    """
    Aggregates multiple metrics into a 0–100 presentation score.
    """
    base_score = 100

    # Deduct points for too many filler words
    filler_penalty = min(filler_analysis.get("total_fillers", 0) * 1.5, 30)

    # Deduct points if pace is off
    wpm = pacing_analysis.get("wpm", 140)
    pace_penalty = 0
    if wpm < 125:
        pace_penalty = 5
    elif wpm > 160:
        pace_penalty = 5

    # Deduct small penalty if grammar issues exist
    grammar_penalty = len(grammar_results) * 0.5 if grammar_results else 0

    final_score = max(0, base_score - filler_penalty - pace_penalty - grammar_penalty)
    return round(final_score, 2)



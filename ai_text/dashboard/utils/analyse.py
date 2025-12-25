import asyncio
import json
from typing import Dict, List
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
from django.utils import timezone
# import language_tool_python  # ✅ Added for grammar analysis
from difflib import ndiff
import html
import whisper

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

def classify_pace_segment(wpm):
    if wpm >= 190:
        return "Fast"
    elif wpm >= 165:
        return "Slightly Fast"
    elif wpm >= 125:
        return "Good"
    else:
        return "Slow"

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

def analyze_grammar_with_claude_sync(text: str) -> Dict:
    """Claude AI grammar analysis - synchronous version for Django"""

    if not text.strip():
        return {
            "corrected": text,
            "inline": html.escape(text),
            "issues": 0,
            "analysis": [],
            "has_correction": False
        }

    if not CLAUDE_AVAILABLE:
        return {
            "corrected": text,
            "inline": html.escape(text),
            "issues": 0,
            "analysis": [],
            "has_correction": False
        }

    api_key = getattr(settings, "ANTHROPIC_API_KEY", None)
    if not api_key:
       return {
            "corrected": corrected_text,
            "inline": inline_html if clean_errors else html.escape(corrected_text),
            "issues": len(grammar_issues),
            "clarity_issues": len(clarity_issues),
            "analysis": clean_errors,
            "has_correction": corrected_text.strip() != text.strip()
        }

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
- Fix true SPOKEN grammar errors only
- Remove IMMEDIATELY repeated sentences or phrases (back-to-back only)

YOU ARE NOT ALLOWED TO:
- Suggest punctuation or spelling variants
- Rewrite sentences for style or flow
- Suggest sentence variety
- Improve wording unless repetition is removed
- Merge or restructure sentences

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

Return ONLY valid JSON:
{{
  "corrected_text": "Corrected spoken transcript",
  "errors": [
    {{
      "original": "exact phrase from transcript",
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
        raw_errors = result.get("errors", [])

        ALLOWED_TYPES = {"article", "verb", "tense", "plural", "pronoun", "clarity"}
        clean_errors = []

        for e in raw_errors:
            original = e.get("original", "").strip()
            correction = e.get("correction", "").strip()
            error_type = e.get("type", "").lower()
            explanation = e.get("explanation", "").lower()

            if error_type not in ALLOWED_TYPES:
                continue

            if original in {",", ".", "and", "but"}:
                continue

            if "consider" in explanation or "style" in explanation:
                continue

            if not original or not correction or original == correction:
                continue

            if original not in text:
                continue

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
        return {
            "corrected": text,
            "inline": html.escape(text),
            "issues": 0,
            "analysis": [],
            "has_correction": False
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

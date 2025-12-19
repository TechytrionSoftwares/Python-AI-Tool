import json
import textwrap
from django.conf import settings
from anthropic import Anthropic  # pip install anthropic

claude_client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)

def seconds_to_timestamp(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"

def ask_claude_for_segments_with_timestamps(timestamped_segments, audio_duration):
    """
    timestamped_segments: [{"start_s": float, "end_s": float, "text": str}]
    Claude v4 (claude-sonnet-4-20250514) compatible version.
    """

    prompt = textwrap.dedent(f"""
    You are an expert speech evaluator.

    You are given the following exact timestamped transcript segments:
    {json.dumps(timestamped_segments)}

    Your tasks:
    1. Merge segments into natural speaking chunks (do NOT modify timestamps).
    2. Use start_s and end_s from input segments — NEVER invent timestamps.
    3. Compute WPM using total words and audio_duration={audio_duration}.
    4. Detect filler words from this fixed list:
       ["um","uh","umm","hmm","like","you know","kind of","sort of","actually","basically","literally","so","well","right"]
    5. Detect questions (sentences ending in "?").

    Return ONLY valid JSON with EXACT structure:

    {{
        "segments": [
            {{
                "start": "MM:SS",
                "end": "MM:SS",
                "start_s": 0.00,
                "end_s": 0.00,
                "text": "..."
            }}
        ],
        "wpm": 0,
        "pace_rating": "slow | normal | fast",
        "filler_words": {{
            "list": [],
            "counts": {{}}
        }},
        "questions": []
    }}
    """)

    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )

        # Extract raw text returned from Claude
        raw = "".join([block.text for block in response.content]).strip()

        # Extract ONLY JSON from the entire message
        start = raw.find("{")
        end = raw.rfind("}") + 1
        json_text = raw[start:end]

        parsed = json.loads(json_text)

        return parsed

    except Exception as e:
        print("Claude error:", e)
        print("RAW OUTPUT:", raw if 'raw' in locals() else None)

        # fallback
        return {
            "segments": timestamped_segments,
            "wpm": 0,
            "pace_rating": "normal",
            "filler_words": {"list": [], "counts": {}},
            "questions": []
        }


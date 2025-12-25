# tasks.py
from celery import shared_task
from .models import Recording

import tempfile, os
from pydub import AudioSegment
from .utils.s3_bucket import download_from_s3, upload_to_s3
from .utils.analyse import transcribe_audio_with_timestamps, analyze_filler_words_from_text, analyze_pacing, analyze_grammar_with_claude_sync, generate_pdf, analyze_hedging_words, analyze_conciseness, adjust_segments_to_audio, classify_pace_segment, mark_pause_segments, highlight_filler_words, highlight_hedging_words, FILLER_WORDS, HEDGING_WORDS
import asyncio
import json
from typing import Dict, List
from urllib.parse import urlparse
from .utils.claude_utils import ask_claude_for_segments_with_timestamps

@shared_task(bind=True)
def process_recording_task(self, recording_id):
    recording = Recording.objects.get(id=recording_id)

    try:
        recording.status = "processing"
        recording.progress = 20
        recording.save()

        # 🔽 DOWNLOAD FROM S3
        temp_original = tempfile.NamedTemporaryFile(delete=False).name
        parsed = urlparse(recording.audio_url)
        s3_key = parsed.path.lstrip("/")
        download_from_s3(s3_key, temp_original)


        recording.progress = 30
        recording.save()

        # 🔽 AUDIO / VIDEO HANDLING (reuse your logic)
        if recording.file_type == "video":
            from moviepy.editor import VideoFileClip
            clip = VideoFileClip(temp_original)
            temp_audio = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
            clip.audio.write_audiofile(temp_audio, codec="pcm_s16le")
            clip.close()
            sound = AudioSegment.from_file(temp_audio)

        else:
            # 🔥 AUDIO FILE HANDLING (THIS WAS MISSING)
            sound = AudioSegment.from_file(temp_original)

        # Normalize audio
        sound = sound.set_channels(1).set_frame_rate(16000)

        temp_wav = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
        sound.export(temp_wav, format="wav")


        audio_duration = len(sound) / 1000

        recording.progress = 45
        recording.save()

        # 🔽 TRANSCRIPTION
        timestamped_segments = transcribe_audio_with_timestamps(temp_wav)
        transcript = " ".join(s["text"] for s in timestamped_segments)

        claude_seg_result = ask_claude_for_segments_with_timestamps(
            timestamped_segments,
            audio_duration
        )

        raw_segments = claude_seg_result.get("segments", [])
        pacing_segments = adjust_segments_to_audio(raw_segments, audio_duration)

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

        for seg in pacing_segments:
            seg["text"] = highlight_filler_words(
                seg["text"],
                FILLER_WORDS,
                seg["start_s"],
                seg["end_s"]
            )

            seg["text"] = highlight_hedging_words(
                seg["text"],
                HEDGING_WORDS,
                seg["start_s"],
                seg["end_s"]
            )




        recording.progress = 60
        recording.save()

        # 🔽 ANALYSIS
        filler = analyze_filler_words_from_text(transcript, audio_duration)
        pacing = analyze_pacing(transcript, audio_duration)
        grammar_data = analyze_grammar_with_claude_sync(transcript)
        grammar_results = grammar_data.get("analysis", [])
        grammar_inline = grammar_data.get("inline", "")
        hedging_data = analyze_hedging_words(transcript, audio_duration)
        conciseness_data = analyze_conciseness(transcript, audio_duration)



        recording.progress = 80
        recording.save()

        # 🔽 PDF
        pdf_path = generate_pdf(transcript)
        pdf_key = f"uploads/pdf/{recording.id}.pdf"
        with open(pdf_path, "rb") as f:
            pdf_url = upload_to_s3(f, pdf_key)

        # 🔽 SAVE RESULTS
        recording.transcript = transcript
        recording.pdf_url = pdf_url
        recording.duration = audio_duration

        recording.filler_data = filler
        recording.pacing_data = {
        "wpm": claude_seg_result.get("wpm", 0),
        **pacing
        }

        recording.pacing_segments = {
        "segments": pacing_segments,
        "pace_segments": pace_segments
        }

        recording.grammar_data = {
        "analysis": grammar_results,
        "inline": grammar_inline
        }

        recording.hedging_data = hedging_data
        recording.conciseness_data = conciseness_data

        recording.status = "completed" if transcript else "failed"
        recording.progress = 10
        recording.save()

        # after transcription
        recording.progress = 40
        recording.save()

        # after grammar + analytics
        recording.progress = 70
        recording.save()

        # final save
        recording.progress = 100
        recording.status = "completed"
        recording.save()


    except Exception as e:
        recording.status = "failed"
        recording.save()
        raise e

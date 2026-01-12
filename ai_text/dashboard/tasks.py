# tasks.py
from celery import shared_task
from .models import Recording

import tempfile, os
from pydub import AudioSegment
from .utils.s3_bucket import download_from_s3, upload_to_s3
from .utils.analyse import (
    transcribe_audio_with_timestamps, 
    analyze_filler_words_from_text, 
    analyze_pacing, 
    analyze_grammar_with_claude_sync, 
    generate_pdf, 
    analyze_hedging_words, 
    analyze_conciseness, 
    adjust_segments_to_audio, 
    classify_pace_segment, 
    mark_pause_segments, 
    highlight_filler_words, 
    highlight_hedging_words, 
    FILLER_WORDS, 
    HEDGING_WORDS
)
import json
from urllib.parse import urlparse
from .utils.claude_utils import ask_claude_for_segments_with_timestamps
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def process_recording_task(self, recording_id):
    recording = Recording.objects.get(id=recording_id)
    
    temp_original = None
    temp_audio = None
    temp_wav = None
    clip = None

    try:
        recording.status = "processing"
        recording.progress = 10
        recording.save()

        # DOWNLOAD FROM S3
        temp_original = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=os.path.splitext(recording.audio_url)[1]
        ).name

        parsed = urlparse(recording.audio_url)
        s3_key = parsed.path.lstrip("/")

        logger.info(f"Downloading from S3: {s3_key}")
        download_from_s3(s3_key, temp_original)

        recording.progress = 20
        recording.save()

        # AUDIO / VIDEO HANDLING
        if recording.file_type == "video":
            logger.info(f"Processing video file: {recording_id}")

            from moviepy.editor import VideoFileClip

            try:
                clip = VideoFileClip(temp_original)
            except Exception as video_error:
                logger.error(f"Cannot open video file: {video_error}")
                recording.status = "failed"
                recording.progress = 100
                recording.save()
                return

            # 🔴 FIX: finalize progress on no-audio
            if clip.audio is None:
                logger.error(f"Video file has no audio track: {recording_id}")
                recording.status = "failed"
                recording.progress = 100
                recording.save()

                clip.close()
                return

            temp_audio = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name

            try:
                clip.audio.write_audiofile(
                    temp_audio,
                    codec="pcm_s16le",
                    logger=None
                )
            except Exception as audio_error:
                logger.error(f"Error extracting audio: {audio_error}")
                recording.status = "failed"
                recording.progress = 100
                recording.save()
                clip.close()
                return

            clip.close()
            clip = None

            try:
                sound = AudioSegment.from_file(temp_audio)
            except Exception as sound_error:
                logger.error(f"Error loading audio: {sound_error}")
                recording.status = "failed"
                recording.progress = 100
                recording.save()
                return

        else:
            # AUDIO FILE HANDLING
            logger.info(f"Processing audio file: {recording_id}")

            try:
                sound = AudioSegment.from_file(temp_original)
            except Exception as e:
                logger.error(f"Error processing audio file: {e}")
                recording.status = "failed"
                recording.progress = 100
                recording.save()
                raise

        recording.progress = 30
        recording.save()

        # Normalize audio
        sound = sound.set_channels(1).set_frame_rate(16000)

        temp_wav = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
        sound.export(temp_wav, format="wav")

        audio_duration = len(sound) / 1000
        logger.info(f"Audio duration: {audio_duration}s")

        recording.progress = 40
        recording.save()

        # TRANSCRIPTION
        logger.info(f"Starting transcription: {recording_id}")
        timestamped_segments = transcribe_audio_with_timestamps(temp_wav)

        if not timestamped_segments:
            logger.error("No transcription generated")
            recording.status = "failed"
            recording.progress = 100
            recording.save()
            return

        transcript = " ".join(s["text"] for s in timestamped_segments)
        recording.progress = 50
        recording.save()

        # CLAUDE SEGMENTS
        claude_seg_result = ask_claude_for_segments_with_timestamps(
            timestamped_segments,
            audio_duration
        )

        raw_segments = claude_seg_result.get("segments", [])
        pacing_segments = adjust_segments_to_audio(raw_segments, audio_duration)

        recording.progress = 60
        recording.save()

        # PACE SEGMENTS
        pace_segments = []
        for seg in pacing_segments:
            words = len(seg["text"].split())
            duration = max(seg["end_s"] - seg["start_s"], 0.1)
            wpm = (words / duration) * 60

            pace_segments.append({
                "label": classify_pace_segment(wpm),
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
                seg["text"], FILLER_WORDS, seg["start_s"], seg["end_s"]
            )
            seg["text"] = highlight_hedging_words(
                seg["text"], HEDGING_WORDS, seg["start_s"], seg["end_s"]
            )

        recording.progress = 70
        recording.save()

        # ANALYSIS
        filler = analyze_filler_words_from_text(transcript, audio_duration)
        pacing = analyze_pacing(transcript, audio_duration)
        grammar_data = analyze_grammar_with_claude_sync(transcript)

        recording.progress = 80
        recording.save()

        # PDF
        pdf_path = generate_pdf(transcript)
        with open(pdf_path, "rb") as f:
            pdf_url = upload_to_s3(f, f"uploads/pdf/{recording.id}.pdf")

        os.unlink(pdf_path)

        recording.progress = 90
        recording.save()

        # SAVE RESULTS
        recording.transcript = transcript
        recording.pdf_url = pdf_url
        recording.duration = audio_duration
        recording.filler_data = filler
        recording.pacing_data = {"wpm": claude_seg_result.get("wpm", 0), **pacing}
        recording.pacing_segments = {
            "segments": pacing_segments,
            "pace_segments": pace_segments
        }
        recording.grammar_data = grammar_data
        recording.hedging_data = analyze_hedging_words(transcript, audio_duration)
        recording.conciseness_data = analyze_conciseness(transcript, audio_duration)

        recording.status = "completed"
        recording.progress = 100
        recording.save()

        logger.info(f"✓ Recording processed successfully: {recording_id}")

    except Exception as e:
        logger.exception(e)
        recording.status = "failed"
        recording.progress = 100
        recording.save()
        raise

    finally:
        if clip:
            try:
                clip.close()
            except:
                pass

        for f in [temp_original, temp_audio, temp_wav]:
            if f and os.path.exists(f):
                try:
                    os.unlink(f)
                except:
                    pass

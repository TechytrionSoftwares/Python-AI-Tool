# dashboard/utils/file_validators.py

import os
import mimetypes

# Allowed MIME types
ALLOWED_AUDIO_MIMES = [
    'audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/wave', 'audio/x-wav',
    'audio/ogg', 'audio/flac', 'audio/aac', 'audio/m4a', 'audio/x-m4a',
    'audio/webm', 'audio/opus', 'audio/wma'
]

ALLOWED_VIDEO_MIMES = [
    'video/mp4', 'video/mpeg', 'video/quicktime', 'video/x-msvideo',
    'video/x-matroska', 'video/webm', 'video/x-flv', 'video/3gpp'
]

ALLOWED_EXTENSIONS = [
    '.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.opus', '.wma',  # Audio
    '.mp4', '.mpeg', '.mpg', '.mov', '.avi', '.mkv', '.webm', '.flv', '.3gp'  # Video
]

MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB


def validate_audio_video_file(uploaded_file):
    """
    Validate audio/video files
    Returns: (is_valid, error_message, file_type)
    """
    
    # 1. Check file size
    if uploaded_file.size > MAX_FILE_SIZE:
        return False, f"File too large. Maximum size is 500MB", None
    
    if uploaded_file.size < 1024:  # Less than 1KB
        return False, "File is too small or empty", None
    
    # 2. Check file extension
    file_name = uploaded_file.name.lower()
    file_ext = os.path.splitext(file_name)[1]
    
    if file_ext not in ALLOWED_EXTENSIONS:
        return False, f"Invalid file type '{file_ext}'. Only audio/video files allowed", None
    
    # 3. Check MIME type from content
    content_type = uploaded_file.content_type.lower() if uploaded_file.content_type else ""
    
    # Determine if audio or video
    is_audio = (
        any(content_type.startswith(mime) for mime in ['audio/']) or
        file_ext in ['.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.opus', '.wma']
    )
    
    is_video = (
        any(content_type.startswith(mime) for mime in ['video/']) or
        file_ext in ['.mp4', '.mpeg', '.mpg', '.mov', '.avi', '.mkv', '.webm', '.flv', '.3gp']
    )
    
    if not is_audio and not is_video:
        return False, "File must be a valid audio or video file", None
    
    file_type = "audio" if is_audio else "video"
    
    # 4. Verify file signature (magic bytes) - basic check
    try:
        uploaded_file.seek(0)
        header = uploaded_file.read(min(12, uploaded_file.size))
        uploaded_file.seek(0)
        
        # Check for common file signatures
        valid_signatures = [
            b'\xff\xfb', b'\xff\xf3', b'\xff\xf2',  # MP3
            b'ID3',  # MP3 with ID3
            b'RIFF',  # WAV/AVI
            b'\x00\x00\x00\x18ftypmp4',  # MP4
            b'\x00\x00\x00\x1cftyp',  # MP4
            b'OggS',  # OGG
            b'fLaC',  # FLAC
            b'\x1a\x45\xdf\xa3',  # MKV/WebM
        ]
        
        is_valid_signature = any(header.startswith(sig) or sig in header for sig in valid_signatures)
        
        if not is_valid_signature:
            # For some formats, we'll be lenient if extension and mime match
            if file_ext in ALLOWED_EXTENSIONS and (is_audio or is_video):
                return True, None, file_type
            return False, "File appears to be corrupted or not a valid media file", None
    
    except Exception as e:
        # If we can't read the file, reject it
        return False, f"Error reading file: {str(e)}", None
    
    return True, None, file_type
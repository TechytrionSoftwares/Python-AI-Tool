# dashboard/utils/id_encoder.py

import base64

def encode_id(num: int) -> str:
    encoded = base64.urlsafe_b64encode(str(num).encode()).decode()
    return encoded.rstrip("=")

def decode_id(encoded: str) -> int:
    padding = "=" * (-len(encoded) % 4)
    decoded = base64.urlsafe_b64decode(encoded + padding).decode()
    return int(decoded)

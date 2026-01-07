from django import template
from dashboard.utils.id_encoder import encode_id

register = template.Library()

@register.filter
def encode_recording_id(value):
    return encode_id(value)

# @register.filter
# def encode_id(value):
#     """
#     Encodes a numeric ID for use in URLs
#     """
#     try:
#         return encode_id(value)
#     except Exception:
#         return ""

# @register.filter(name="encode_id")
# def encode_id_filter(value):
#     """
#     Encodes a numeric ID for use in URLs
#     """
#     try:
#         return _encode_id(value)
#     except Exception:
#         return None       

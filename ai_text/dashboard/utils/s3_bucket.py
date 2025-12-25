import boto3, os, tempfile, time, re
from django.conf import settings
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
from django.db import models
from django.contrib.auth.models import User

class SpeechReport(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    transcript = models.TextField()
    pdf_url = models.URLField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.created_at.strftime('%Y-%m-%d')}"
        
class Recording(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recordings")
    title = models.CharField(max_length=255)

    audio_url = models.URLField(blank=True, null=True)
    pdf_url = models.URLField(blank=True, null=True)
    transcript = models.TextField(blank=True, null=True)

    filler_data = models.JSONField(default=dict, blank=True)
    pacing_data = models.JSONField(default=dict, blank=True)
    grammar_data = models.JSONField(default=dict, blank=True)
    pacing_segments = models.JSONField(default=dict, blank=True)

    hedging_data = models.JSONField(default=dict, blank=True)
    conciseness_data = models.JSONField(default=dict, blank=True)
    summary_data = models.JSONField(default=dict, blank=True)

    duration = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    STATUS_CHOICES = (
        ("uploading", "Uploading"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="uploading")
    progress = models.PositiveIntegerField(default=0)

    FILE_TYPE_CHOICES = (
        ("audio", "Audio"),
        ("video", "Video"),
    )
    file_type = models.CharField(max_length=10, choices=FILE_TYPE_CHOICES, default="audio")

    def __str__(self):
        return f"{self.title} ({self.user.username})"
import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ai_text.settings")

app = Celery("ai_text")

app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# 🔒 Process ONE file at a time
app.conf.worker_concurrency = 1
app.conf.worker_prefetch_multiplier = 1

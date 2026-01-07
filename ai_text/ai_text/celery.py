import os
from celery import Celery
from celery.schedules import crontab

#  Set Django settings FIRST
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ai_text.settings")

#  Create Celery app
app = Celery("ai_text")

#  Load config from Django settings
app.config_from_object("django.conf:settings", namespace="CELERY")

#  Auto-discover tasks
app.autodiscover_tasks()

#  Celery Beat schedule (AFTER app is created)
app.conf.beat_schedule = {
    "deactivate-expired-subscriptions-every-15-min": {
        "task": "dashboard.tasks.subscription_tasks.deactivate_expired_subscriptions",
        "schedule": crontab(minute="*/15"),
    },
}

#  Process ONE task at a time (your choice)
app.conf.worker_concurrency = 1
app.conf.worker_prefetch_multiplier = 1

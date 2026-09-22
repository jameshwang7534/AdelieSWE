"""CLI entry point: celery -A app.workers.celery_app:app worker."""

from app.core.config import Settings
from app.workers.factory import create_celery_app

app = create_celery_app(Settings())

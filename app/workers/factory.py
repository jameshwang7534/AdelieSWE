"""Build isolated Celery applications without opening network/database connections."""

from celery import Celery
from kombu import Queue

from app.core.config import Settings
from app.workers.tasks import PING_TASK_NAME, ping
from app.workers.workspaces import PREPARE_TASK_NAME, prepare_workspace


def create_celery_app(settings: Settings) -> Celery:
    if settings.celery_broker_url is None or settings.celery_result_backend is None:
        raise ValueError("CELERY_BROKER_URL and CELERY_RESULT_BACKEND are required for workers")
    application = Celery(
        "platform",
        broker=settings.celery_broker_url.get_secret_value(),
        backend=settings.celery_result_backend.get_secret_value(),
        set_as_current=False,
    )
    timeout = settings.dependency_timeout_seconds
    application.conf.update(
        task_queues=tuple(Queue(name) for name in ("orchestration", "indexing", "agents")),
        task_default_queue="orchestration",
        task_routes={
            PING_TASK_NAME: {"queue": "orchestration"},
            PREPARE_TASK_NAME: {"queue": "orchestration"},
        },
        task_create_missing_queues=False,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        result_accept_content=["json"],
        enable_utc=True,
        timezone="UTC",
        task_track_started=True,
        task_soft_time_limit=20,
        task_time_limit=30,
        result_expires=3600,
        result_backend_thread_safe=True,
        worker_prefetch_multiplier=1,
        worker_log_level=settings.log_level,
        worker_log_format="[%(asctime)s: %(levelname)s/%(processName)s] %(message)s",
        worker_task_log_format=(
            "[%(asctime)s: %(levelname)s] %(task_name)s[%(task_id)s]: %(message)s"
        ),
        broker_connection_retry_on_startup=True,
        broker_connection_retry=True,
        broker_connection_max_retries=5,
        broker_connection_timeout=timeout,
        broker_transport_options={
            "socket_connect_timeout": timeout,
            "socket_timeout": timeout,
            "retry_on_timeout": False,
            "visibility_timeout": 3600,
            "max_retries": 2,
        },
        task_publish_retry=True,
        task_publish_retry_policy={
            "max_retries": 2,
            "interval_start": 0,
            "interval_step": 0.2,
            "interval_max": 0.5,
        },
        redis_socket_connect_timeout=timeout,
        redis_socket_timeout=timeout,
        redis_retry_on_timeout=False,
        result_backend_always_retry=True,
        result_backend_max_retries=2,
        result_backend_transport_options={"retry_policy": {"max_retries": 2}},
    )
    application.task(
        bind=True,
        name=PING_TASK_NAME,
        autoretry_for=(ConnectionError, TimeoutError),
        retry_backoff=True,
        retry_backoff_max=10,
        retry_jitter=True,
        max_retries=3,
    )(ping)
    application.task(
        name=PREPARE_TASK_NAME,
        soft_time_limit=900,
        time_limit=960,
        max_retries=0,
    )(prepare_workspace)
    return application

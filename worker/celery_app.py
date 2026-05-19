from celery import Celery
from config.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "tier1_worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,       # one task at a time per worker
    task_soft_time_limit=300,           # 5 min soft limit
    task_time_limit=360,                # 6 min hard kill
    task_routes={
        "worker.tasks.run_tier1_checks": {"queue": "tier1"},
    },
)

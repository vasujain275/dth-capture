"""DTH Capture entry point.

Wires up all services and starts ffmpeg + watcher threads.

Usage:
    python -m app.main
"""

import threading
import time

from app.config import settings
from app.services.minio_service import MinioService
from app.services.mongodb_service import MongoDBService
from app.services.rabbitmq_publisher import RabbitMQPublisher
from app.capture.ffmpeg import run_ffmpeg, stale_segment_watchdog
from app.capture.watcher import run_uploader
from app.utils.logging import get_logger

logger = get_logger(__name__)


def main():
    """Wire up services and start capture + upload pipeline."""
    logger.info("=" * 60)
    logger.info("DTH Capture & Upload (v0.2)")
    logger.info(f"  Channel  : {settings.CHANNEL_NAME}")
    logger.info(f"  Video    : {settings.VIDEO_DEV} @ {settings.RESOLUTION}p{settings.FRAMERATE}")
    logger.info(f"  Encoder  : libx264 preset={settings.X264_PRESET} crf={settings.VIDEO_CRF}")
    logger.info(f"  Audio    : {settings.AUDIO_CARD} @ {settings.AUDIO_BITRATE}")
    logger.info(f"  Segments : {settings.SEGMENT_SECS}s each → .mkv clips")
    logger.info(f"  MinIO    : {settings.MINIO_ENDPOINT} / {settings.MINIO_BUCKET_NAME}")
    logger.info(f"  MongoDB  : {settings.MONGODB_URI} / {settings.MONGODB_DATABASE}")
    logger.info(f"  RabbitMQ : {settings.RABBITMQ_HOST}:{settings.RABBITMQ_PORT} / {settings.RABBITMQ_QUEUE}")
    logger.info("=" * 60)

    # Initialize services
    minio_svc = MinioService(settings)
    minio_svc.ensure_bucket()

    mongo_svc = MongoDBService(settings)
    rabbit_svc = RabbitMQPublisher(settings)

    # ffmpeg capture thread (daemon — dies with main process)
    ffmpeg_thread = threading.Thread(
        target=run_ffmpeg, args=(settings,), daemon=True, name="ffmpeg"
    )
    ffmpeg_thread.start()

    # Stale segment watchdog thread
    watchdog_thread = threading.Thread(
        target=stale_segment_watchdog, args=(settings,), daemon=True, name="watchdog"
    )
    watchdog_thread.start()

    # Give ffmpeg a moment to start writing before watcher starts
    time.sleep(3)

    # Uploader runs in main thread (blocks until KeyboardInterrupt)
    try:
        run_uploader(settings, minio_svc, mongo_svc, rabbit_svc)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        rabbit_svc.close()
        mongo_svc.close()


if __name__ == "__main__":
    main()

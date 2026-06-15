"""Segment watcher and uploader for DTH capture.

Responsibilities:
- Validate completed .mkv segments before upload
- Watch segments/ directory for new files via watchdog
- Upload to MinIO, create MongoDB record, publish RabbitMQ job
- Mark bad segments for inspection

Usage:
    from app.capture.watcher import run_uploader
    from app.config import settings

    run_uploader(settings, minio_service, mongodb_service, rabbitmq_publisher)
"""

import json
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.schemas.job import DiarizationJob
from app.services.minio_service import MinioService
from app.services.mongodb_service import MongoDBService
from app.services.rabbitmq_publisher import RabbitMQPublisher
from app.utils.logging import get_logger

logger = get_logger(__name__)

# ── Segments directory ────────────────────────────────────────────────────────
SEGMENTS_DIR = Path(__file__).resolve().parent.parent.parent / "segments"


def validate_segment(filepath: Path, min_segment_secs: int) -> bool:
    """Validate a completed .mkv segment before upload.

    Checks: file exists, minimum size, ffprobe validation (video+audio streams),
    and minimum duration.

    Args:
        filepath: Path to the .mkv file
        min_segment_secs: Minimum acceptable duration in seconds

    Returns:
        True if segment is valid and ready for upload
    """
    if not filepath.exists():
        logger.warning(f"[validate] File disappeared: {filepath.name}")
        return False

    if filepath.stat().st_size < 1024 * 1024:
        mark_bad_segment(filepath, "File too small")
        return False

    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(filepath),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        mark_bad_segment(filepath, "ffprobe timed out")
        return False
    except Exception as e:
        mark_bad_segment(filepath, f"ffprobe failed: {e}")
        return False

    if result.returncode != 0:
        err = result.stderr.strip() or "unknown ffprobe error"
        mark_bad_segment(filepath, f"ffprobe rejected file: {err}")
        return False

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        mark_bad_segment(filepath, f"ffprobe returned invalid JSON: {e}")
        return False

    streams = info.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not has_video:
        mark_bad_segment(filepath, "No video stream")
        return False
    if not has_audio:
        mark_bad_segment(filepath, "No audio stream")
        return False

    duration_raw = info.get("format", {}).get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError):
        mark_bad_segment(filepath, "Could not read duration")
        return False

    if duration < min_segment_secs:
        mark_bad_segment(
            filepath,
            f"Segment too short: {duration:.1f}s < {min_segment_secs}s",
        )
        return False

    logger.info(
        f"[validate] ✓ {filepath.name}: {duration:.1f}s, "
        f"{filepath.stat().st_size / 1024 / 1024:.1f} MB, audio+video"
    )
    return True


def mark_bad_segment(filepath: Path, reason: str) -> None:
    """Rename bad segment to .mkv.bad for inspection.

    Keeps the file on disk but prevents endless upload retries.

    Args:
        filepath: Path to the bad segment file
        reason: Human-readable reason for rejection
    """
    bad_path = filepath.with_suffix(filepath.suffix + ".bad")
    try:
        filepath.rename(bad_path)
        logger.error(f"[validate] ✗ {reason}. Moved bad segment: {bad_path.name}")
    except FileNotFoundError:
        logger.warning(f"[validate] File disappeared before marking bad: {filepath.name}")
    except Exception as e:
        logger.error(f"[validate] ✗ {reason}. Failed to mark bad segment {filepath.name}: {e}")


def parse_recorded_at(filename: str) -> datetime:
    """Parse recorded_at timestamp from segment filename.

    Expected format: {channel}_YYYYMMDD_HHMMSS.mkv

    Args:
        filename: Segment filename

    Returns:
        Parsed datetime in UTC, or utcnow if parsing fails
    """
    # Strip .mkv suffix
    stem = Path(filename).stem  # e.g. "cnbc-awaaz_20260615_120728"
    parts = stem.rsplit("_", 2)  # ["cnbc-awaaz", "20260615", "120728"]
    try:
        date_str = parts[-2]  # "20260615"
        time_str = parts[-1]  # "120728"
        dt_str = f"{date_str}{time_str}"
        return datetime.strptime(dt_str, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        logger.warning(f"Could not parse recorded_at from filename: {filename}, using utcnow")
        return datetime.now(timezone.utc)


class SegmentHandler(FileSystemEventHandler):
    """Handles new .mkv files written by ffmpeg's segment muxer.

    ffmpeg closes the file when a segment is complete — we queue it for upload.
    """

    def __init__(self, upload_queue: list):
        self._queue = upload_queue
        self._lock = threading.Lock()

    def _handle(self, path: str | bytes) -> None:
        if isinstance(path, bytes):
            path = path.decode()
        if not path.endswith(".mkv"):
            return
        filepath = Path(path)
        if not filepath.exists():
            return
        # Ignore tiny files — segment still being written or empty
        if filepath.stat().st_size < 1024:
            return
        with self._lock:
            if path not in self._queue:
                logger.info(f"[watcher] Queued for upload: {filepath.name}")
                self._queue.append(path)

    def on_closed(self, event):
        """inotify IN_CLOSE_WRITE — file fully written and closed by ffmpeg."""
        if not event.is_directory:
            self._handle(event.src_path)


def run_uploader(
    settings,
    minio_service: MinioService,
    mongodb_service: MongoDBService,
    rabbitmq_publisher: RabbitMQPublisher,
) -> None:
    """Watch segments/ directory and process completed .mkv files.

    Flow for each valid segment:
    1. Validate segment (ffprobe check)
    2. Upload to MinIO
    3. Create MongoDB clip record (status=pending)
    4. Publish DiarizationJob to RabbitMQ
    5. Delete local file

    If MongoDB or RabbitMQ fails after MinIO upload, the local file is kept
    for retry on next run.

    Args:
        settings: Settings instance
        minio_service: MinIO service for uploads
        mongodb_service: MongoDB service for clip records
        rabbitmq_publisher: RabbitMQ publisher for jobs
    """
    # Process leftover .mkv files from previous runs (e.g., after reboot)
    SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)
    leftover_mkv = sorted(SEGMENTS_DIR.glob("*.mkv"))
    if leftover_mkv:
        logger.info(f"[upload] Found {len(leftover_mkv)} leftover .mkv segment(s) from previous run")
        for f in leftover_mkv:
            if validate_segment(f, settings.MIN_SEGMENT_SECS):
                _process_segment(f, settings, minio_service, mongodb_service, rabbitmq_publisher)

    upload_queue: list[str] = []
    handler = SegmentHandler(upload_queue)
    observer = Observer()
    observer.schedule(handler, str(SEGMENTS_DIR), recursive=False)
    observer.start()
    logger.info(f"[watcher] Watching {SEGMENTS_DIR}")

    try:
        while True:
            if upload_queue:
                mkv_path = Path(upload_queue.pop(0))
                if validate_segment(mkv_path, settings.MIN_SEGMENT_SECS):
                    _process_segment(mkv_path, settings, minio_service, mongodb_service, rabbitmq_publisher)
            else:
                time.sleep(0.2)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        observer.stop()
        observer.join()


def _process_segment(
    filepath: Path,
    settings,
    minio_service: MinioService,
    mongodb_service: MongoDBService,
    rabbitmq_publisher: RabbitMQPublisher,
) -> None:
    """Upload, record, and publish a single segment.

    Args:
        filepath: Path to validated .mkv segment
        settings: Settings instance
        minio_service: MinIO service
        mongodb_service: MongoDB service
        rabbitmq_publisher: RabbitMQ publisher
    """
    filename = filepath.name
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    object_key = f"{settings.CHANNEL_NAME}/{date_str}/{filename}"

    # 1. Upload to MinIO
    if not minio_service.upload_file(filepath, object_key):
        logger.error(f"MinIO upload failed for {filename}, will retry on next run")
        return

    # 2. Create MongoDB clip record
    try:
        doc_id = mongodb_service.create_clip_record(
            object_name=object_key,
            channel=settings.CHANNEL_NAME,
            bucket_name=settings.MINIO_BUCKET_NAME,
        )
    except Exception as e:
        logger.error(f"MongoDB insert failed for {filename} (uploaded to MinIO): {e}")
        logger.error("File kept locally for retry")
        return

    # 3. Publish RabbitMQ job
    recorded_at = parse_recorded_at(filename)
    job = DiarizationJob(
        job_id=str(uuid.uuid4()),
        object_id=doc_id,
        minio_bucket=settings.MINIO_BUCKET_NAME,
        minio_key=object_key,
        channel=settings.CHANNEL_NAME,
        recorded_at=recorded_at,
    )

    if not rabbitmq_publisher.publish_job(job):
        logger.error(f"RabbitMQ publish failed for {filename} (uploaded to MinIO, record in MongoDB)")
        logger.error("File kept locally for retry")
        return

    # 4. Success — delete local file
    filepath.unlink()
    logger.info(f"[upload] ✓ Complete: {filename} → {object_key} (doc={doc_id}, job={job.job_id})")

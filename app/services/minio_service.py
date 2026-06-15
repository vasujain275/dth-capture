"""MinIO service for DTH capture uploads.

Responsibilities:
- Initialize MinIO client from settings
- Ensure bucket exists
- Upload segment files with retries

Usage:
    from app.services.minio_service import MinioService
    from app.config import settings

    service = MinioService(settings)
    service.ensure_bucket()
    success = service.upload_file("/path/to/segment.mkv", "cnbc-awaaz/20260615/segment.mkv")
"""

import time
from pathlib import Path

from minio import Minio
from minio.error import S3Error

from app.utils.logging import get_logger

logger = get_logger(__name__)


class MinioService:
    """Handles MinIO client lifecycle and uploads for DTH capture."""

    def __init__(self, settings):
        self.bucket_name = settings.MINIO_BUCKET_NAME
        self.retries = settings.UPLOAD_RETRIES
        self.retry_delay = settings.UPLOAD_RETRY_DELAY
        self.client = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )
        logger.info(f"MinIO client initialized (endpoint={settings.MINIO_ENDPOINT}, bucket={self.bucket_name})")

    def ensure_bucket(self) -> None:
        """Create bucket if it does not exist."""
        if not self.client.bucket_exists(self.bucket_name):
            self.client.make_bucket(self.bucket_name)
            logger.info(f"Created bucket: {self.bucket_name}")
        else:
            logger.info(f"Bucket exists: {self.bucket_name}")

    def upload_file(self, local_path: str | Path, object_key: str) -> bool:
        """Upload a file to MinIO with retries.

        Args:
            local_path: Path to local file to upload
            object_key: MinIO object key (e.g., "cnbc-awaaz/20260615/file.mkv")

        Returns:
            True on success, False after all retries exhausted
        """
        local_path = Path(local_path)
        filename = local_path.name

        for attempt in range(1, self.retries + 1):
            try:
                file_size = local_path.stat().st_size
                logger.info(
                    f"Upload attempt {attempt}/{self.retries}: "
                    f"{filename} ({file_size / 1024 / 1024:.1f} MB) → {self.bucket_name}/{object_key}"
                )
                self.client.fput_object(
                    bucket_name=self.bucket_name,
                    object_name=object_key,
                    file_path=str(local_path),
                    content_type="video/x-matroska",
                )
                logger.info(f"Upload success: {self.bucket_name}/{object_key}")
                return True

            except S3Error as e:
                logger.error(f"MinIO S3 error on attempt {attempt}: {e}")
            except FileNotFoundError:
                logger.warning(f"File disappeared before upload: {filename}")
                return False
            except Exception as e:
                logger.error(f"Unexpected error on attempt {attempt}: {e}")

            if attempt < self.retries:
                delay = self.retry_delay * attempt  # progressive backoff
                logger.info(f"Retrying in {delay}s...")
                time.sleep(delay)

        logger.error(
            f"Upload failed after {self.retries} attempts: {filename}. "
            f"File kept locally for retry."
        )
        return False

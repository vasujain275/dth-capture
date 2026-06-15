"""Application configuration via environment variables using pydantic-settings."""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """DTH Capture configuration.

    All fields can be overridden via environment variables or .env file.
    """

    # ── Capture Device ──────────────────────────────────────────────────────────
    VIDEO_DEV: str = Field(
        default="/dev/video0", description="V4L2 video device path"
    )
    AUDIO_CARD: str = Field(
        default="hw:3,0", description="ALSA audio card identifier"
    )
    CHANNEL_NAME: str = Field(
        default="cnbc-awaaz", description="Channel name for folder/record grouping"
    )

    # ── ffmpeg encode settings ──────────────────────────────────────────────────
    RESOLUTION: str = Field(default="1280x720", description="Video resolution WxH")
    FRAMERATE: int = Field(default=30, description="Video framerate")
    X264_PRESET: str = Field(default="veryfast", description="x264 encoding preset")
    VIDEO_CRF: int = Field(default=28, description="Video CRF quality (lower=better)")
    SEGMENT_SECS: int = Field(default=60, description="Segment duration in seconds")
    AUDIO_BITRATE: str = Field(default="96k", description="Audio bitrate")

    # ── Segment validation ──────────────────────────────────────────────────────
    MIN_SEGMENT_SECS: int = Field(
        default=55, description="Minimum segment duration before upload"
    )
    STALE_WARN_SECS: int = Field(
        default=90, description="Warn if no new segment after this many seconds"
    )

    # ── MinIO ───────────────────────────────────────────────────────────────────
    MINIO_ENDPOINT: str = Field(
        default="192.168.1.10:9000", description="MinIO server endpoint"
    )
    MINIO_ACCESS_KEY: str = Field(
        default="minioadmin", description="MinIO access key"
    )
    MINIO_SECRET_KEY: str = Field(
        default="minioadmin", description="MinIO secret key"
    )
    MINIO_BUCKET_NAME: str = Field(
        default="dth-chunks", description="MinIO bucket name"
    )
    MINIO_SECURE: bool = Field(
        default=False, description="Use HTTPS for MinIO connection"
    )

    # ── Upload retry ────────────────────────────────────────────────────────────
    UPLOAD_RETRIES: int = Field(
        default=5, description="Number of upload retry attempts"
    )
    UPLOAD_RETRY_DELAY: int = Field(
        default=5, description="Base delay between retries in seconds"
    )

    # ── MongoDB ─────────────────────────────────────────────────────────────────
    MONGODB_URI: str = Field(
        default="mongodb://192.168.1.10:27017",
        description="MongoDB connection URI",
    )
    MONGODB_DATABASE: str = Field(
        default="dth", description="MongoDB database name"
    )

    # ── RabbitMQ ────────────────────────────────────────────────────────────────
    RABBITMQ_HOST: str = Field(
        default="192.168.1.10", description="RabbitMQ host"
    )
    RABBITMQ_PORT: int = Field(default=5672, description="RabbitMQ port")
    RABBITMQ_QUEUE: str = Field(
        default="diarization-jobs", description="Queue name for job messages"
    )
    RABBITMQ_USERNAME: str = Field(
        default="guest", description="RabbitMQ username"
    )
    RABBITMQ_PASSWORD: str = Field(
        default="guest", description="RabbitMQ password"
    )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()

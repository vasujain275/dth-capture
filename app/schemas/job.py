"""RabbitMQ job message schema.

Must match the DiarizationJob schema in stock-market-ai/app/schemas/job.py.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class DiarizationJob(BaseModel):
    """Message published to RabbitMQ by capture service.

    Consumed by the GPU worker in stock-market-ai.
    """

    job_id: str = Field(description="Unique job identifier (UUID)")
    object_id: str = Field(description="MongoDB document _id (hex string)")
    minio_bucket: str = Field(description="MinIO bucket containing the clip")
    minio_key: str = Field(
        description="Object key in MinIO (e.g., 'cnbc-awaaz/20260612/cnbc-awaaz_20260612_120728.mkv')"
    )
    channel: str = Field(
        description="Channel name (e.g., 'cnbc-awaaz', 'cnbc-tv18')"
    )
    recorded_at: datetime = Field(description="When the clip was recorded")

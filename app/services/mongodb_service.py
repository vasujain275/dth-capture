"""MongoDB service for clip metadata management.

Responsibilities:
- Initialize MongoDB client and connection
- Create clip records with initial "pending" status
- Ensure indexes for efficient queries

Usage:
    from app.services.mongodb_service import MongoDBService
    from app.config import settings

    service = MongoDBService(settings)
    doc_id = service.create_clip_record("cnbc-awaaz/20260615/file.mkv", "cnbc-awaaz", "dth-chunks")
    service.close()
"""

from datetime import datetime, timezone

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.utils.logging import get_logger

logger = get_logger(__name__)


class MongoDBService:
    """Handles MongoDB operations for clip metadata in DTH capture."""

    def __init__(self, settings):
        self.client = MongoClient(settings.MONGODB_URI)
        self.db = self.client[settings.MONGODB_DATABASE]
        self.collection = self.db["clips"]

        self._ensure_indexes()
        logger.info(f"MongoDB client initialized (uri={settings.MONGODB_URI}, db={settings.MONGODB_DATABASE})")

    def _ensure_indexes(self) -> None:
        """Create indexes for efficient queries."""
        try:
            # Unique index on object_name to prevent duplicates
            self.collection.create_index("object_name", unique=True)
            # Compound index for channel + timestamp queries
            self.collection.create_index([("channel", 1), ("timestamp", -1)])
            # Index for status-based queries
            self.collection.create_index("status")
            logger.info("MongoDB indexes ensured")
        except PyMongoError as e:
            logger.error(f"Failed to create indexes: {e}")

    def create_clip_record(
        self,
        object_name: str,
        channel: str,
        bucket_name: str,
    ) -> str:
        """Create a new clip record in MongoDB with "pending" status.

        Args:
            object_name: Full object path in MinIO (e.g., "cnbc-awaaz/20260615/file.mkv")
            channel: Channel name
            bucket_name: MinIO bucket name

        Returns:
            Document ID as hex string

        Raises:
            PyMongoError: If insert fails
        """
        now = datetime.now(timezone.utc)

        document = {
            "object_name": object_name,
            "channel": channel,
            "bucket_name": bucket_name,
            "timestamp": now,
            "status": "pending",
            "transcript_devanagari": None,
            "transcript_roman": None,
            "transcript_english": None,
            "transcript_stats": None,
            "created_at": now,
            "updated_at": now,
        }

        try:
            result = self.collection.insert_one(document)
            doc_id = str(result.inserted_id)
            logger.info(f"Created clip record: {doc_id} for {object_name}")
            return doc_id
        except PyMongoError as e:
            logger.error(f"Failed to create clip record for {object_name}: {e}")
            raise

    def close(self) -> None:
        """Close MongoDB connection."""
        self.client.close()
        logger.info("MongoDB connection closed")

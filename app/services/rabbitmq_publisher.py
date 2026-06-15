"""RabbitMQ publisher for diarization jobs.

Responsibilities:
- Create connections (lazy, reconnect on failure)
- Publish DiarizationJob messages with PERSISTENT delivery

Usage:
    from app.services.rabbitmq_publisher import RabbitMQPublisher
    from app.config import settings

    publisher = RabbitMQPublisher(settings)
    publisher.publish_job(job)
    publisher.close()
"""

import json
import ssl
import pika

from app.schemas.job import DiarizationJob
from app.utils.logging import get_logger

logger = get_logger(__name__)


class RabbitMQPublisher:
    """Publishes DiarizationJob messages to RabbitMQ.

    Creates a new connection for each publish call to handle dropped connections
    on Raspberry Pi (Wi-Fi flakiness, broker restarts, etc.).
    """

    def __init__(self, settings):
        self.host = settings.RABBITMQ_HOST
        self.port = settings.RABBITMQ_PORT
        self.queue = settings.RABBITMQ_QUEUE
        self.username = settings.RABBITMQ_USERNAME
        self.password = settings.RABBITMQ_PASSWORD
        self.vhost = settings.RABBITMQ_VHOST
        self.use_ssl = settings.RABBITMQ_SSL
        self._connection = None
        self._channel = None

        proto = "AMQPS" if self.use_ssl else "AMQP"
        logger.info(f"RabbitMQ publisher initialized ({proto} {self.host}:{self.port}, queue={self.queue})")

    def _get_channel(self) -> pika.channel.Channel:
        """Get a channel, creating a new connection if needed."""
        if self._connection and self._connection.is_open and self._channel and self._channel.is_open:
            return self._channel

        # Close stale connection if any
        self._close_connection()

        credentials = pika.PlainCredentials(self.username, self.password)

        ssl_options = None
        if self.use_ssl:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            ssl_options = pika.SSLOptions(ssl_context, self.host)

        params = pika.ConnectionParameters(
            host=self.host,
            port=self.port,
            credentials=credentials,
            virtual_host=self.vhost,
            ssl_options=ssl_options,
            heartbeat=600,
            blocked_connection_timeout=300,
        )
        self._connection = pika.BlockingConnection(params)
        self._channel = self._connection.channel()
        self._channel.queue_declare(queue=self.queue, durable=True)
        logger.info(f"RabbitMQ connection established to {self.host}:{self.port}")
        return self._channel

    def _close_connection(self) -> None:
        """Close connection if open, swallowing errors."""
        try:
            if self._connection and self._connection.is_open:
                self._connection.close()
        except Exception:
            pass
        self._connection = None
        self._channel = None

    def publish_job(self, job: DiarizationJob) -> bool:
        """Publish a DiarizationJob to RabbitMQ.

        Args:
            job: DiarizationJob instance to publish

        Returns:
            True on success, False on failure
        """
        try:
            channel = self._get_channel()
            body = job.model_dump_json()
            channel.basic_publish(
                exchange="",
                routing_key=self.queue,
                body=body.encode("utf-8"),
                properties=pika.BasicProperties(
                    delivery_mode=pika.DeliveryMode.Persistent,
                    content_type="application/json",
                ),
            )
            logger.info(f"Published job {job.job_id} to queue '{self.queue}'")
            return True

        except Exception as e:
            logger.error(f"Failed to publish job {job.job_id}: {e}")
            self._close_connection()
            return False

    def close(self) -> None:
        """Close RabbitMQ connection."""
        self._close_connection()
        logger.info("RabbitMQ connection closed")

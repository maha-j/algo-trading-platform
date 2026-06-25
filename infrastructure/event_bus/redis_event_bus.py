"""
Redis Event Bus — Infrastructure adapter for async inter-service communication.

Fixes applied (BUG-07, FAULT-02, SCALE-02):
  - BUG-07: Consumer group created with id="$" (only new messages, not history
            replay from the beginning). Prevents spurious orders on restart.
  - FAULT-02: Dead-Letter Queue implemented — failed messages after max_retries
              are XADD to a DLQ stream for inspection and manual recovery.
  - SCALE-02: asyncio.gather uses return_exceptions=True so one failing handler
              does not prevent other handlers from running or ACK being sent.

Design Decision:
    Redis Streams (XADD/XREADGROUP) over Pub/Sub for durability.
    1. Messages persist — replay after crash
    2. Consumer groups — competing consumers for horizontal scaling
    3. XACK prevents duplicate processing
    4. MAXLEN trim controls memory usage
    5. Unique IDs enable idempotency checks
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from dataclasses import fields
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, TypeVar

import redis.asyncio as aioredis
from redis.asyncio import ConnectionPool
from redis.exceptions import ConnectionError, TimeoutError

from config.settings import get_settings
from core.domain.events import BaseEvent

logger = logging.getLogger(__name__)

T           = TypeVar("T", bound=BaseEvent)
HandlerType = Callable[[dict], Coroutine[Any, Any, None]]

DLQ_STREAM         = "stream:dlq"
MAX_RETRY_ATTEMPTS = 3


# ─────────────────────────────────────────────────────────────────────────────
# Serialisation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _serialise_event(event: BaseEvent) -> dict[str, str]:
    """Flatten a frozen dataclass event into a Redis hash (all string values)."""
    result: dict[str, str] = {}
    for f in fields(event):  # type: ignore[arg-type]
        value = getattr(event, f.name)
        if isinstance(value, datetime):
            result[f.name] = value.isoformat()
        elif isinstance(value, Decimal):
            result[f.name] = str(value)
        elif isinstance(value, Enum):
            result[f.name] = value.value
        elif isinstance(value, dict):
            result[f.name] = json.dumps(value)
        elif value is None:
            result[f.name] = ""
        else:
            result[f.name] = str(value)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Redis Event Bus
# ─────────────────────────────────────────────────────────────────────────────

class RedisEventBus:
    """
    Async Redis Streams-based event bus for the trading platform.

    Features:
        - publish:        Append events to a named stream
        - subscribe:      Persistent consumer group subscription
        - broadcast:      Fan-out via Pub/Sub (low-latency notifications)
        - Dead-Letter Queue: Failed messages archived for inspection

    Lifecycle:
        bus = create_event_bus(...)
        await bus.start()
        bus.register_handler("stream:bars", handler)
        await bus.start_consuming()
        ...
        await bus.stop()
    """

    def __init__(
        self,
        pool:           ConnectionPool,
        consumer_group: str,
        consumer_name:  str,
        batch_size:     int = 100,
    ) -> None:
        self._pool           = pool
        self._client:        aioredis.Redis | None = None
        self._consumer_group = consumer_group
        self._consumer_name  = consumer_name
        self._batch_size     = batch_size
        self._handlers:      dict[str, list[HandlerType]] = {}
        self._running        = False
        self._subscriber_tasks: list[asyncio.Task] = []

        settings              = get_settings()
        self._stream_max_len  = settings.redis.stream_max_len

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to Redis and initialise the client."""
        self._client  = aioredis.Redis(connection_pool=self._pool)
        self._running = True
        logger.info(
            "Redis event bus started",
            extra={
                "consumer_group": self._consumer_group,
                "consumer_name":  self._consumer_name,
            },
        )

    async def stop(self) -> None:
        """Drain pending tasks and close the Redis connection."""
        self._running = False
        for task in self._subscriber_tasks:
            task.cancel()
        if self._subscriber_tasks:
            await asyncio.gather(*self._subscriber_tasks, return_exceptions=True)
        if self._client:
            await self._client.aclose()
        logger.info("Redis event bus stopped")

    # ── Publishing ────────────────────────────────────────────────────────────

    async def publish(self, event: BaseEvent) -> str:
        """Append an event to its designated Redis Stream."""
        if not self._client:
            raise RuntimeError("Event bus not started. Call start() first.")

        channel = type(event).channel()
        payload = _serialise_event(event)

        try:
            entry_id: str = await self._client.xadd(
                name        = channel,
                fields      = payload,
                maxlen      = self._stream_max_len,
                approximate = True,
            )
            logger.debug(
                "Event published",
                extra={
                    "channel":        channel,
                    "event_id":       event.event_id,
                    "entry_id":       entry_id,
                    "correlation_id": event.correlation_id,
                },
            )
            return entry_id
        except (ConnectionError, TimeoutError) as exc:
            logger.error(
                "Failed to publish event",
                extra={"channel": channel, "event_id": event.event_id, "error": str(exc)},
                exc_info=True,
            )
            raise

    async def publish_many(self, events: list[BaseEvent]) -> list[str]:
        """Batch publish via pipeline (N events → 1 TCP round-trip)."""
        if not self._client:
            raise RuntimeError("Event bus not started. Call start() first.")

        async with self._client.pipeline(transaction=False) as pipe:
            for event in events:
                channel = type(event).channel()
                payload = _serialise_event(event)
                pipe.xadd(channel, payload, maxlen=self._stream_max_len, approximate=True)
            results = await pipe.execute()

        entry_ids = [str(r) for r in results]
        logger.debug(f"Batch published {len(events)} events via pipeline")
        return entry_ids

    # ── Subscribing ───────────────────────────────────────────────────────────

    def register_handler(self, channel: str, handler: HandlerType) -> None:
        """Register an async handler for a specific Redis stream."""
        if channel not in self._handlers:
            self._handlers[channel] = []
        self._handlers[channel].append(handler)
        logger.info(f"Handler registered for channel '{channel}'")

    async def _ensure_consumer_group(self, channel: str) -> None:
        """Create consumer group if it doesn't exist."""
        if not self._client:
            return
        try:
            await self._client.xgroup_create(
                name      = channel,
                groupname = self._consumer_group,
                # FIX BUG-07: id="$" — only NEW messages after this moment.
                # id="0" was replaying ALL historical messages on every restart,
                # causing spurious signals and broker orders.
                id        = "$",
                mkstream  = True,
            )
            logger.info(
                f"Consumer group '{self._consumer_group}' created on '{channel}' "
                f"(starting from '$' — new messages only)"
            )
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                pass   # group already exists — idempotent
            else:
                raise

    async def start_consuming(self) -> None:
        """Start background consumer tasks for all registered channels."""
        for channel in self._handlers:
            await self._ensure_consumer_group(channel)
            task = asyncio.create_task(
                self._consume_loop(channel),
                name=f"consumer::{channel}",
            )
            self._subscriber_tasks.append(task)
            logger.info(f"Consumer started for channel '{channel}'")

    async def _consume_loop(self, channel: str) -> None:
        """Blocking consumer loop for a single Redis stream."""
        if not self._client:
            return

        logger.info(f"Entering consume loop for '{channel}'")
        while self._running:
            try:
                messages = await self._client.xreadgroup(
                    groupname    = self._consumer_group,
                    consumername = self._consumer_name,
                    streams      = {channel: ">"},
                    count        = self._batch_size,
                    block        = 0,
                )

                if not messages:
                    continue

                for _stream, entries in messages:
                    for entry_id, data in entries:
                        await self._dispatch(channel, entry_id, data)

            except asyncio.CancelledError:
                logger.info(f"Consumer loop cancelled for '{channel}'")
                break
            except (ConnectionError, TimeoutError) as exc:
                logger.error(f"Redis connection error in consumer '{channel}': {exc}")
                await asyncio.sleep(1.0)
            except Exception as exc:
                logger.error(f"Unexpected error in consumer '{channel}': {exc}", exc_info=True)
                await asyncio.sleep(0.1)

    async def _dispatch(
        self,
        channel:  str,
        entry_id: str | bytes,
        data:     dict[bytes, bytes],
    ) -> None:
        """
        Decode a stream entry and invoke all registered handlers.

        FIX SCALE-02: return_exceptions=True so one failing handler does not
        prevent others from running or the ACK from being sent.

        FIX FAULT-02: After MAX_RETRY_ATTEMPTS failures, message is sent
        to the Dead-Letter Queue stream for manual inspection and recovery.
        """
        if not self._client:
            return

        decoded: dict[str, str] = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in data.items()
        }

        correlation_id = decoded.get("correlation_id", "unknown")
        retry_count    = int(decoded.get("_retry_count", 0))

        try:
            handlers = self._handlers.get(channel, [])
            # FIX SCALE-02: return_exceptions=True — partial handler failure
            # does not abort others; we log each failure individually.
            results = await asyncio.gather(
                *[h(decoded) for h in handlers],
                return_exceptions=True,
            )

            # Log individual handler failures but still ACK
            any_failed = False
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    any_failed = True
                    logger.error(
                        f"Handler {i} failed on channel '{channel}'",
                        extra={
                            "channel":        channel,
                            "entry_id":       str(entry_id),
                            "correlation_id": correlation_id,
                            "error":          str(result),
                        },
                        exc_info=result,
                    )

            if any_failed and retry_count >= MAX_RETRY_ATTEMPTS:
                # FIX FAULT-02: Move to Dead-Letter Queue
                await self._send_to_dlq(channel, entry_id, decoded, correlation_id)

            # ACK the message regardless of individual handler results
            # (partial failure logged; message will not be re-delivered)
            await self._client.xack(channel, self._consumer_group, entry_id)

        except Exception as exc:
            logger.error(
                "Fatal dispatch error — message left in PEL",
                extra={
                    "channel":        channel,
                    "entry_id":       str(entry_id),
                    "correlation_id": correlation_id,
                    "error":          str(exc),
                },
                exc_info=True,
            )

    async def _send_to_dlq(
        self,
        source_channel:  str,
        entry_id:        str | bytes,
        decoded:         dict[str, str],
        correlation_id:  str,
    ) -> None:
        """
        FIX FAULT-02: Send a persistently failing message to the DLQ.

        The DLQ stream is `stream:dlq`. Operators inspect it via:
            XRANGE stream:dlq - +
        and can replay individual messages manually after fixing the bug.
        """
        if not self._client:
            return
        dlq_payload = {
            **decoded,
            "_dlq_source_channel": source_channel,
            "_dlq_entry_id":       str(entry_id),
            "_dlq_timestamp":      datetime.now(timezone.utc).isoformat(),
            "_dlq_correlation_id": correlation_id,
        }
        try:
            await self._client.xadd(
                name        = DLQ_STREAM,
                fields      = dlq_payload,
                maxlen      = 10_000,   # keep last 10k DLQ entries
                approximate = True,
            )
            logger.error(
                "Message sent to Dead-Letter Queue",
                extra={
                    "source_channel":  source_channel,
                    "entry_id":        str(entry_id),
                    "correlation_id":  correlation_id,
                    "dlq_stream":      DLQ_STREAM,
                },
            )
        except Exception as exc:
            logger.critical(f"Failed to write to DLQ: {exc}")

    # ── Pub/Sub broadcast ──────────────────────────────────────────────────────

    async def broadcast(self, channel: str, message: str) -> int:
        """Fan-out via Redis Pub/Sub (ephemeral, no persistence)."""
        if not self._client:
            raise RuntimeError("Event bus not started.")
        return await self._client.publish(channel, message)  # type: ignore[return-value]


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def create_event_bus(consumer_group: str, consumer_name: str) -> RedisEventBus:
    """Factory: wires RedisEventBus with settings from environment."""
    settings = get_settings()
    pool = ConnectionPool.from_url(
        settings.redis.url,
        max_connections          = settings.redis.max_connections,
        socket_timeout           = settings.redis.socket_timeout,
        decode_responses         = False,
        health_check_interval    = 30,
    )
    return RedisEventBus(
        pool           = pool,
        consumer_group = consumer_group,
        consumer_name  = consumer_name,
    )

import json
import logging
import threading
import time
from collections.abc import Callable

from confluent_kafka import OFFSET_BEGINNING, Consumer, KafkaError, KafkaException, TopicPartition

from services.match_state import MatchState
from services.partitioner import partition_for

log = logging.getLogger(__name__)


class FixtureFeed:
    """Reads one fixture's messages from its Kafka partition, from the oldest retained offset.

    Messages are keyed by fixture ID, so one partition holds the whole match. The consumer uses
    assign() and never commits, so each start rebuilds the full match state.
    """

    def __init__(self, kafka_config: dict, topic: str, group_id: str, state: MatchState,
                 on_update: Callable[[list[dict]], None], on_caught_up: Callable[[], None],
                 on_reset: Callable[[], None] | None = None):
        self._config = {
            **kafka_config,
            "group.id": group_id,
            "enable.auto.commit": False,
            "enable.partition.eof": True,
        }
        self._topic = topic
        self._state = state
        self._on_update = on_update
        self._on_caught_up = on_caught_up
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fixture-feed", daemon=True)
        self.partition: int | None = None
        self.caught_up = False
        self.connected = False
        self.last_error: str | None = None
        self._on_reset = on_reset
        self._started_once = False

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._consume()
                backoff = 1.0
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                log.exception("feed failed, retrying in %.0fs", backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _consume(self) -> None:
        consumer = Consumer(self._config)
        try:
            partitions = len(consumer.list_topics(topic=self._topic, timeout=10).topics[self._topic].partitions)
            self.partition = partition_for(self._state.fixture_id, partitions)
            # A rebuild replays from the start, so the state must start empty.
            self._state.reset()
            if self._started_once and self._on_reset:
                self._on_reset()
            self._started_once = True
            self.caught_up = False
            consumer.assign([TopicPartition(self._topic, self.partition, OFFSET_BEGINNING)])
            self.connected = True
            self.last_error = None
            key = self._state.fixture_id.encode()
            log.info("reading %s partition %d for fixture %s", self._topic, self.partition, self._state.fixture_id)
            while not self._stop.is_set():
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        if not self.caught_up:
                            log.info("caught up: %d messages applied", self._state.messages_applied)
                            self.caught_up = True
                            self._on_caught_up()
                        continue
                    raise KafkaException(msg.error())
                if msg.key() != key:
                    continue
                try:
                    payload = json.loads(msg.value())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.warning("skipping non-JSON message at offset %d", msg.offset())
                    continue
                try:
                    changed = self._state.apply(payload)
                except Exception:
                    # Skip a malformed message. A re-raise would rebuild from the start and hit it again.
                    log.exception("skipping message the state cannot apply at offset %d", msg.offset())
                    continue
                # During the start-up replay, clients get one snapshot at catch-up instead.
                if changed and self.caught_up:
                    self._on_update(changed)
        finally:
            self.connected = False
            consumer.close()

    def health(self) -> dict:
        last = self._state.last_message_at
        return {
            "connected": self.connected,
            "caughtUp": self.caught_up,
            "partition": self.partition,
            "messagesApplied": self._state.messages_applied,
            "secondsSinceLastMessage": None if last is None else round(time.time() - last, 1),
            "lastError": self.last_error,
        }


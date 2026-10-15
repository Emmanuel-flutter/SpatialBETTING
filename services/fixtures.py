import json
import logging
import threading
import time
from dataclasses import dataclass, field

from confluent_kafka import OFFSET_BEGINNING, Consumer, KafkaError, KafkaException, TopicPartition

from services import opta
from services.match_state import STALE_AFTER, _event_time, _quiet_status, _status

log = logging.getLogger(__name__)



def _parse_start(value: str) -> float | None:
    """The fixture startTime ("2026-10-15T14:50:00Z") in epoch seconds, or None."""
    return _event_time(value.removesuffix("Z")) if value else None


@dataclass
class FixtureSummary:
    id: str
    partition: int
    home: str = ""
    away: str = ""
    start_time: str = ""
    competition_id: str = ""
    tournament_id: str = ""
    first_ms: int | None = None
    last_ms: int | None = None
    messages: int = 0
    side_by_id: dict[str, str] = field(default_factory=dict)
    # Event id -> (seqId, typeId, periodId, side, ownGoal, gameEnd). Only what status and score need.
    events: dict[int, tuple] = field(default_factory=dict)

    def apply(self, payload: dict, kafka_ms: int | None) -> None:
        body = payload.get("body", {})
        fixture = body.get("fixture") or {}
        if fixture:
            self.start_time = fixture.get("startTime", self.start_time)
            self.competition_id = fixture.get("competitionId", self.competition_id)
            self.tournament_id = fixture.get("tournamentId", self.tournament_id)
            for t in fixture.get("teams", []):
                side = {"HOME": "home", "AWAY": "away"}.get(t.get("association"))
                if side:
                    setattr(self, side, t.get("name", ""))
                    self.side_by_id[t.get("id", "")] = side
        for e in body.get("content", {}).get("liveData", {}).get("matchDetails", {}).get("event", []):
            current = self.events.get(e["id"])
            if current is not None and current[0] > e["seqId"]:
                continue
            q = {x["qualifierId"] for x in e.get("qualifier", [])}
            self.events[e["id"]] = (e["seqId"], e["typeId"], e.get("periodId"), self.side_by_id.get(e.get("contestantId", "")),
                                    opta.Q_OWN_GOAL in q, opta.Q_GAME_END in q)
        self.messages += 1
        if kafka_ms is not None:
            self.first_ms = kafka_ms if self.first_ms is None else min(self.first_ms, kafka_ms)
            self.last_ms = kafka_ms if self.last_ms is None else max(self.last_ms, kafka_ms)

    def to_json(self, now: float) -> dict:
        started: set[int] = set()
        ended: dict[int, bool] = {}
        score = {"home": 0, "away": 0}
        for _, type_id, period, side, own_goal, game_end in self.events.values():
            if type_id in opta.DELETED_TYPES:
                continue
            if type_id == opta.PERIOD_START:
                started.add(period)
            elif type_id == opta.PERIOD_END:
                ended[period] = ended.get(period, False) or game_end
            elif type_id == opta.GOAL and side:
                score[("away" if side == "home" else "home") if own_goal else side] += 1
        # Line-ups come long before kick-off, so a quiet pre-match feed is stale only once kick-off is well past.
        kicked_off = _parse_start(self.start_time)
        stale_pre = kicked_off is not None and now - kicked_off > STALE_AFTER
        status = _quiet_status(_status(started, ended), None if self.last_ms is None else self.last_ms / 1000, now,
                               pre=stale_pre)
        return {
            "fixtureId": self.id, "home": self.home, "away": self.away, "startTime": self.start_time,
            "competitionId": self.competition_id, "tournamentId": self.tournament_id,
            "status": status, "score": score, "messages": self.messages, "partition": self.partition,
            "firstMessageAt": None if self.first_ms is None else self.first_ms / 1000,
            "lastMessageAt": None if self.last_ms is None else self.last_ms / 1000,
        }


class FixtureIndex:
    """Reads every partition of the topic from the oldest retained offset and keeps a small summary
    per fixture, for the games list. Uses assign() and never commits."""

    def __init__(self, kafka_config: dict, topic: str, group_id: str):
        self._config = {**kafka_config, "group.id": group_id, "enable.auto.commit": False, "enable.partition.eof": True}
        self._topic = topic
        self._lock = threading.Lock()
        self._fixtures: dict[str, FixtureSummary] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fixture-index", daemon=True)
        self.partitions = 0
        self.caught_up = False
        self.connected = False
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def list(self) -> list[dict]:
        now = time.time()
        with self._lock:
            return [f.to_json(now) for f in self._fixtures.values()]

    def get(self, fixture_id: str) -> dict | None:
        with self._lock:
            f = self._fixtures.get(fixture_id)
            return f.to_json(time.time()) if f else None

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._consume()
                backoff = 1.0
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                log.exception("fixture index failed, retrying in %.0fs", backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _consume(self) -> None:
        consumer = Consumer(self._config)
        try:
            self.partitions = len(consumer.list_topics(topic=self._topic, timeout=10).topics[self._topic].partitions)
            with self._lock:
                self._fixtures.clear()
            self.caught_up = False
            pending = set(range(self.partitions))
            consumer.assign([TopicPartition(self._topic, p, OFFSET_BEGINNING) for p in range(self.partitions)])
            self.connected = True
            self.last_error = None
            log.info("indexing %s, %d partitions", self._topic, self.partitions)
            while not self._stop.is_set():
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        pending.discard(msg.partition())
                        if not pending and not self.caught_up:
                            self.caught_up = True
                            log.info("fixture index caught up: %d fixtures", len(self._fixtures))
                        continue
                    raise KafkaException(msg.error())
                key = msg.key()
                if not key:
                    continue
                try:
                    payload = json.loads(msg.value())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                kind, ms = msg.timestamp()
                fixture_id = key.decode(errors="replace")
                with self._lock:
                    summary = self._fixtures.get(fixture_id)
                    if summary is None:
                        summary = self._fixtures[fixture_id] = FixtureSummary(fixture_id, msg.partition())
                    try:
                        summary.apply(payload, ms if kind else None)
                    except Exception:
                        log.exception("skipping message the index cannot apply at %d:%d", msg.partition(), msg.offset())
        finally:
            self.connected = False
            consumer.close()

    def health(self) -> dict:
        return {"connected": self.connected, "caughtUp": self.caught_up, "partitions": self.partitions,
                "fixtures": len(self._fixtures), "lastError": self.last_error}

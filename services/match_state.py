import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

from services import opta

PITCH_EVENT_LIMIT = 250
# Clients run the clock between events for at most this many match seconds, then hold it. A feed
# that stops mid-half (no full-time event) leaves the clock still, not running on.
CLOCK_HOLD = 180
# A match that has started but sent nothing for this long is treated as over. Four captured
# fixtures stop at minute 92-97 with no full-time event (scan of 2026-10-14).
STALE_AFTER = 30 * 60
TIMELINE_TYPES = frozenset(
    opta.SHOTS | {opta.CARD, opta.PLAYER_OFF, opta.PLAYER_ON, opta.PERIOD_START, opta.PERIOD_END,
                  opta.TEMP_GOAL, opta.START_DELAY}
)


def _qualifiers(event: dict) -> dict[int, Any]:
    return {q["qualifierId"]: q.get("value") for q in event.get("qualifier", [])}


def _event_time(value: str | None) -> float | None:
    """Opta event timeStamp has no zone suffix; delivery-lag measurement on live data shows it is UTC."""
    try:
        parsed = datetime.fromisoformat(value)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Team:
    id: str = ""
    name: str = ""
    kit: str | None = None
    starters: list[str] = field(default_factory=list)
    subs: list[str] = field(default_factory=list)


class MatchState:
    """Match state for one fixture, rebuilt from Opta MA18DP event deltas.

    Opta gives x/y relative to the acting team attacking left to right. The state rotates the
    away team's coordinates by 180 degrees, so the home team always attacks to the right.
    """

    def __init__(self, fixture_id: str):
        self.fixture_id = fixture_id
        # Match seconds per real second. 1 live; a demo played faster (or paused) sets it, so clients
        # run the clock at the same rate between events.
        self.clock_rate = 1
        # Real ends for display, when the source knows them (the demo does; Opta live does not):
        # {"homeAttacksLeft": [periods]}. The data itself always has the home team attacking right.
        self.orientation: dict | None = None
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._raw: dict[int, dict] = {}
            self._start_time = ""
            self._teams: dict[str, Team] = {"home": Team(), "away": Team()}
            self._side_by_id: dict[str, str] = {}
            self._clock_anchor: tuple[int, int, int, float] | None = None
            self.messages_applied = 0
            self.last_message_at: float | None = None

    def apply(self, message: dict) -> list[dict] | None:
        """Apply one Kafka message. Return the normalised events that changed, or None if ignored."""
        body = message.get("body", {})
        fixture = body.get("fixture") or {}
        if fixture.get("fixtureId") not in (None, self.fixture_id):
            return None
        events = body.get("content", {}).get("liveData", {}).get("matchDetails", {}).get("event", [])

        with self._lock:
            self._apply_fixture(fixture)
            changed = []
            for event in events:
                current = self._raw.get(event["id"])
                if current is not None and current["seqId"] > event["seqId"]:
                    continue
                self._raw[event["id"]] = event
                self._apply_side_effects(event)
                changed.append(self._normalise(event))
            self.messages_applied += 1
            self.last_message_at = time.time()
            return changed

    def snapshot(self) -> dict:
        with self._lock:
            events = sorted(
                (self._normalise(e) for e in self._raw.values()),
                key=lambda e: (e["periodId"] or 0, e["min"] or 0, e["sec"] or 0, e["id"]),
            )
            live = [e for e in events if not e["deleted"]]
            pitch = [e for e in live if e["x"] is not None]
            return {
                **self._summary(live),
                "teams": {side: self._team_json(t) for side, t in self._teams.items()},
                "timeline": [e for e in live if e["typeId"] in TIMELINE_TYPES],
                "pitch": pitch[-PITCH_EVENT_LIMIT:],
            }

    def summary(self) -> dict:
        with self._lock:
            live = [e for e in map(self._normalise, self._raw.values()) if not e["deleted"]]
            return {
                **self._summary(live),
                "teams": {side: self._team_json(t) for side, t in self._teams.items()},
            }

    def _apply_fixture(self, fixture: dict) -> None:
        if not fixture:
            return
        self._start_time = fixture.get("startTime", self._start_time)
        for t in fixture.get("teams", []):
            side = {"HOME": "home", "AWAY": "away"}.get(t.get("association"))
            if side:
                self._teams[side].id = t.get("id", "")
                self._teams[side].name = t.get("name", "")
                self._side_by_id[t.get("id", "")] = side

    def _apply_side_effects(self, event: dict) -> None:
        side = self._side_by_id.get(event.get("contestantId", ""))
        team = self._teams.get(side) if side else None
        if team and event.get("primaryKitColour"):
            team.kit = event["primaryKitColour"]
        if team and event["typeId"] == opta.TEAM_SETUP:
            self._apply_lineup(team, event)
        if event["typeId"] not in opta.DELETED_TYPES and event.get("periodId") in (1, 2):
            anchor = (event["periodId"], event["timeMin"], event["timeSec"])
            if self._clock_anchor is None or anchor >= self._clock_anchor[:3]:
                self._clock_anchor = (*anchor, time.time())

    @staticmethod
    def _apply_lineup(team: Team, event: dict) -> None:
        raw = {q["qualifierId"]: q for q in event.get("qualifier", [])}
        names = [n.strip() for n in (raw.get(opta.Q_INVOLVED, {}).get("detail") or "").split(",")]
        slots = [s.strip() for s in (raw.get(opta.Q_FORMATION_SLOTS, {}).get("value") or "").split(",")]
        if not names or len(names) != len(slots):
            return
        team.starters = [n for n, s in zip(names, slots) if s != "0"]
        team.subs = [n for n, s in zip(names, slots) if s == "0"]

    def _summary(self, live: list[dict]) -> dict:
        score = {"home": 0, "away": 0}
        stats = {s: {"shots": 0, "corners": 0, "fouls": 0, "yellow": 0, "red": 0} for s in ("home", "away")}
        started: set[int] = set()
        ended: dict[int, bool] = {}
        last_ts = max((e["ts"] for e in live if e["ts"] is not None), default=None)
        for e in live:
            side, t = e["team"], e["typeId"]
            if t == opta.PERIOD_START:
                started.add(e["periodId"])
            elif t == opta.PERIOD_END:
                ended[e["periodId"]] = ended.get(e["periodId"], False) or e["gameEnd"]
            if side is None:
                continue
            if t == opta.GOAL:
                score[_other(side) if e["ownGoal"] else side] += 1
            if t in opta.SHOTS and not e["ownGoal"]:
                stats[side]["shots"] += 1
            elif t == opta.CORNER and e["outcome"] == 1:
                stats[side]["corners"] += 1
            elif t == opta.FOUL and e["outcome"] == 0:
                stats[side]["fouls"] += 1
            elif t == opta.CARD:
                if e["card"] == "yellow":
                    stats[side]["yellow"] += 1
                elif e["card"] in ("second_yellow", "red"):
                    stats[side]["red"] += 1
        return {
            "fixtureId": self.fixture_id,
            "startTime": self._start_time,
            # A paused demo sends nothing and is not stale.
            "status": _quiet_status(_status(started, ended), last_ts, time.time(), pre=False) if self.clock_rate else
                      _status(started, ended),
            "clock": self._clock_json(),
            "score": score,
            "stats": stats,
            "serverTime": time.time(),
            "seq": self.messages_applied,
            "orientation": self.orientation,
        }

    def set_clock_rate(self, rate: float) -> None:
        """Change the clock rate from now on. The anchor moves to the current match time first, so time
        already run at the old rate is kept and none is counted at the new one."""
        with self._lock:
            if self._clock_anchor is not None:
                period, minute, second, seen_at = self._clock_anchor
                now = time.time()
                total = minute * 60 + second + max(0.0, now - seen_at) * self.clock_rate
                self._clock_anchor = (period, int(total // 60), int(total % 60), now)
            self.clock_rate = rate

    def _clock_json(self) -> dict | None:
        if self._clock_anchor is None:
            return None
        period, minute, second, seen_at = self._clock_anchor
        return {"periodId": period, "min": minute, "sec": second, "seenAt": seen_at, "rate": self.clock_rate,
                "holdSec": CLOCK_HOLD}

    def _team_json(self, team: Team) -> dict:
        return {"id": team.id, "name": team.name, "kit": team.kit, "starters": team.starters, "subs": team.subs}

    def _normalise(self, event: dict) -> dict:
        q = _qualifiers(event)
        side = self._side_by_id.get(event.get("contestantId", ""))
        x, y = event.get("x"), event.get("y")
        # The feed sends exactly (0, 0) for events with no position: cards, subs, period markers.
        if x == 0 and y == 0:
            x = y = None
        end_x, end_y = _to_float(q.get(opta.Q_END_X)), _to_float(q.get(opta.Q_END_Y))
        if side == "away":
            x, y = _rotate(x), _rotate(y)
            end_x, end_y = _rotate(end_x), _rotate(end_y)
        type_id = event["typeId"]
        card = None
        if type_id == opta.CARD:
            card = ("second_yellow" if opta.Q_SECOND_YELLOW in q else
                    "red" if opta.Q_RED in q else
                    "yellow" if opta.Q_YELLOW in q else None)
        return {
            "id": event["id"],
            "seqId": event["seqId"],
            "typeId": type_id,
            "name": opta.EVENT_NAMES.get(type_id, f"Event {type_id}"),
            "periodId": event.get("periodId"),
            "min": event.get("timeMin", 0),
            "sec": event.get("timeSec", 0),
            "team": side,
            "player": event.get("playerName"),
            "outcome": event.get("outcome"),
            "x": x if type_id not in opta.DELETED_TYPES else None,
            "y": y if type_id not in opta.DELETED_TYPES else None,
            "endX": end_x,
            "endY": end_y,
            "deleted": type_id in opta.DELETED_TYPES,
            "ownGoal": opta.Q_OWN_GOAL in q,
            "penalty": opta.Q_PENALTY in q,
            "goalConfirmed": opta.Q_GOAL_CONFIRMED in q,
            "possibleVar": opta.Q_POSSIBLE_VAR in q or opta.Q_VAR_DELAY in q,
            "gameEnd": opta.Q_GAME_END in q,
            # Where a shot crossed the goal line: y across the goal, z height. Both 0-100, attacker's frame.
            "goalMouthY": _to_float(q.get(opta.Q_GOAL_MOUTH_Y)),
            "goalMouthZ": _to_float(q.get(opta.Q_GOAL_MOUTH_Z)),
            "ts": _event_time(event.get("timeStamp")),
            "card": card,
        }


def _rotate(value: float | None) -> float | None:
    return None if value is None else round(100 - value, 1)


def _other(side: str) -> str:
    return "away" if side == "home" else "home"


def _status(started: set[int], ended: dict[int, bool]) -> dict:
    if any(ended.values()):
        return {"phase": "ft", "label": "Full time", "periodId": max(ended)}
    if not started:
        return {"phase": "pre", "label": "Pre-match", "periodId": None}
    current = max(started)
    if current in ended:
        return {"phase": "break", "label": "Half time" if current == 1 else f"End of period {current}",
                "periodId": current}
    label = {1: "1st half", 2: "2nd half"}.get(current, f"Period {current}")
    return {"phase": "live", "label": label, "periodId": current}


def _quiet_status(status: dict, last_seen: float | None, now: float, pre: bool = True) -> dict:
    """The status, with a started match that has sent nothing for STALE_AFTER seconds marked as ended."""
    if last_seen is None or now - last_seen <= STALE_AFTER:
        return status
    if status["phase"] in ("live", "break"):
        return {"phase": "ended", "label": "Feed stopped", "periodId": status["periodId"]}
    if status["phase"] == "pre" and pre:
        return {"phase": "ended", "label": "No longer updating", "periodId": None}
    return status

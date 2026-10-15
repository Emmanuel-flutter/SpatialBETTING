import bisect
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

from services.match_state import MatchState

log = logging.getLogger(__name__)

SPEEDS = (0, 1, 2, 4, 8)  # 0 = paused
TICK = 0.1  # seconds between steps of the demo clock
STATUS_EVERY = 1.0


class DemoFeed:
    """Plays a demo match file (built by tools/build_demo.py) into a MatchState on the real clock.

    Same interface as FixtureFeed, plus control(): pause, speed (1x to 8x) and seek (rewind or skip).
    Events are stamped with the current time when sent, so rounds settle exactly as on the live feed.
    A seek rebuilds the state at the new point and calls on_reset first, because rounds that were
    running cannot settle fairly across a jump. With a ball track (tools/build_ball.py) it also sends
    the ball position and whether the ball is in play.
    """

    def __init__(self, path: str, state: MatchState, on_update: Callable[[list[dict]], None],
                 on_caught_up: Callable[[], None], on_reset: Callable[[], None] | None = None,
                 ball_path: str | None = None, on_ball: Callable[[dict], None] | None = None,
                 on_status: Callable[[dict], None] | None = None):
        self._path, self._ball_path = path, ball_path
        self._state = state
        self._on_update, self._on_caught_up, self._on_reset = on_update, on_caught_up, on_reset
        self._on_ball, self._on_status = on_ball, on_status
        self._commands: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="demo-feed", daemon=True)
        self.caught_up = False
        self.connected = False
        self.finished = False
        self.last_error: str | None = None
        self.position = 0.0
        # The demo waits paused before kick-off, so Who Where When (a pre-match pot) can be played. Play
        # starts the match; "restart" goes back here.
        self.speed = 0
        self.length = 0.0

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def control(self, action: str, value) -> None:
        """Called from the event loop. The feed thread applies it on its next step."""
        if action == "speed" and type(value) is int and value in SPEEDS:
            self._commands.put(("speed", value))
        elif action == "seek" and isinstance(value, (int, float)) and not isinstance(value, bool):
            self._commands.put(("seek", float(value)))
        elif action == "skip" and isinstance(value, (int, float)) and not isinstance(value, bool):
            self._commands.put(("skip", float(value)))
        elif action == "restart":
            self._commands.put(("restart", None))

    def _run(self) -> None:
        try:
            with open(self._path) as f:
                demo = json.load(f)
            ball = []
            if self._ball_path:
                with open(self._ball_path) as f:
                    ball = json.load(f)["samples"]
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            self.last_error = str(exc)
            log.exception("cannot load demo %s", self._path)
            return
        self._fixture = demo["fixture"]
        self._state.orientation = {"homeAttacksLeft": demo.get("homeAttacksLeft", [])}
        self._events = demo["events"]
        self._times = [item["t"] for item in self._events]
        self._ball = ball
        self._ball_times = [s[0] for s in ball]
        self.length = max(self._times[-1], self._ball_times[-1] if ball else 0)
        self._rebuild(0.0, notify=False)
        self.connected = True
        log.info("demo %s started", self._fixture["fixtureId"])
        last_wall, last_status, last_ball = time.time(), 0.0, None
        while not self._stop.is_set():
            self._stop.wait(TICK)
            now = time.time()
            self.position = min(self.length, self.position + (now - last_wall) * self.speed)
            last_wall = now
            while not self._commands.empty():
                kind, value = self._commands.get_nowait()
                if kind == "speed":
                    self.speed = value
                    self._state.set_clock_rate(value)
                    # No event may follow for a while (a pause), so send the new clock now.
                    if self.caught_up:
                        self._on_update([])
                elif kind == "restart":
                    self.speed = 0
                    self._rebuild(0.0, notify=True)
                    self._state.set_clock_rate(0)
                    last_ball = None
                else:
                    target = value if kind == "seek" else self.position + value
                    self._rebuild(max(0.0, min(self.length, target)), notify=True)
                    last_ball = None
                last_status = 0.0
            self._advance(self.position)
            if self._ball:
                sample = self._ball_at(self.position)
                if sample is not None and sample != last_ball:
                    last_ball = sample
                    if self._on_ball:
                        poss = {1: "home", 2: "away"}.get(sample[4]) if len(sample) > 4 else None
                        self._on_ball({"x": sample[1], "y": sample[2], "live": bool(sample[3]), "possession": poss})
            self.finished = self.position >= self.length
            if self._on_status and now - last_status >= STATUS_EVERY:
                last_status = now
                self._on_status(self.status())

    def _rebuild(self, target: float, notify: bool) -> None:
        """Reset the match and apply every event up to `target` at once."""
        if notify and self._on_reset:
            self._on_reset()
        self._state.reset()
        self._state.clock_rate = self.speed
        self._state.apply(_message(self._fixture, []))
        self._next = 0
        self.caught_up = False
        self.position = target
        upto = bisect.bisect_right(self._times, target)
        batch = []
        for item in self._events[:upto]:
            batch.append({**item["event"], "timeStamp": _now_stamp()})
        if batch:
            self._state.apply(_message(self._fixture, batch))
        self._next = upto
        # Clients get the rebuilt match in one snapshot. Before kick-off this is the line-ups (they are at t = 0),
        # so a demo waiting paused before kick-off is caught up too.
        self.caught_up = True
        self._on_caught_up()

    def _advance(self, position: float) -> None:
        while self._next < len(self._events) and self._events[self._next]["t"] <= position:
            item = self._events[self._next]
            self._next += 1
            event = {**item["event"], "timeStamp": _now_stamp()}
            changed = self._state.apply(_message(self._fixture, [event]))
            if changed and self.caught_up:
                self._on_update(changed)

    def _ball_at(self, position: float) -> list | None:
        i = bisect.bisect_right(self._ball_times, position) - 1
        if i < 0:
            return None
        # The track has gaps (stoppages between halves); do not show a stale ball across one.
        return self._ball[i] if position - self._ball_times[i] <= 2.0 else None

    def status(self) -> dict:
        return {"position": round(self.position, 1), "length": round(self.length, 1), "speed": self.speed,
                "finished": self.finished}

    def health(self) -> dict:
        last = self._state.last_message_at
        return {"connected": self.connected, "caughtUp": self.caught_up, "demo": True, **self.status(),
                "messagesApplied": self._state.messages_applied,
                "secondsSinceLastMessage": None if last is None else round(time.time() - last, 1),
                "lastError": self.last_error}


def _message(fixture: dict, events: list[dict]) -> dict:
    return {"body": {"fixture": fixture, "content": {"liveData": {"matchDetails": {"event": events}}}}}


def _now_stamp() -> str:
    # Opta timeStamp form: UTC with no zone suffix (see match_state._event_time).
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]

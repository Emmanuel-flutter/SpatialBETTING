"""Where the ball is over time: drawn on the board, and used for the demo penalty's column. No market
settles on it.

Two sources, one shape. Each sample is (t, x, y, live, off) in wall-clock seconds and pitch percent, with
the home team attacking right (the frame of match_state and roulette.cells_at).
- "tracking": the demo's 5 Hz DFL ball track. A dead ball at the edge of the pitch, or past it, is off.
- "events": live Opta. There is no ball track, so the path is estimated: a straight line from each
  positioned event to the next. Out and corner awarded put the ball off the pitch; a foul, a goal, an
  offside or a delay stop play on it. The next normal event puts the ball back in play.
"""
import bisect
from dataclasses import dataclass

from services import opta

OFF_TYPES = frozenset({5, opta.CORNER})  # Out, Corner awarded
STOPPAGE_TYPES = frozenset({opta.FOUL, opta.GOAL, 2, opta.START_DELAY})  # foul, goal, offside pass, start delay
EDGE = 1.0  # pitch percent: a dead tracked ball this close to a line, or past it, is off the pitch
KEEP_SECONDS = 600


@dataclass(frozen=True, order=True)
class Sample:
    t: float
    x: float
    y: float
    live: bool
    off: bool


class BallTrack:
    """Single-threaded: call only from the event loop."""

    def __init__(self, source: str):
        if source not in ("tracking", "events"):
            raise ValueError(f"unknown ball source {source!r}")
        self.source = source
        self._samples: list[Sample] = []
        self._by_event: dict[int, tuple[int, Sample]] = {}  # Opta event id -> (seqId, its sample)

    def add(self, t: float, x: float, y: float, live: bool, off: bool = False) -> None:
        bisect.insort(self._samples, Sample(t, x, y, live, off))
        if self._samples[0].t < t - KEEP_SECONDS:
            del self._samples[:bisect.bisect_left(self._samples, Sample(t - KEEP_SECONDS, -1, -1, False, False))]

    def add_tracking(self, t: float, x: float, y: float, live: bool) -> None:
        off = not live and (x <= EDGE or x >= 100 - EDGE or y <= EDGE or y >= 100 - EDGE)
        self.add(t, x, y, live, off)

    def _drop(self, sample: Sample) -> None:
        i = bisect.bisect_left(self._samples, sample)
        if i < len(self._samples) and self._samples[i] == sample:
            del self._samples[i]

    def add_events(self, events: list[dict]) -> None:
        """Opta events, normalised by match_state. They can come late or out of order; the samples stay in
        time order. A re-sent event (higher seqId) replaces its sample, and a deleted event removes it."""
        for e in events:
            seq = e.get("seqId", 0)
            held = self._by_event.get(e["id"])
            if held is not None and held[0] > seq:
                continue
            if held is not None:
                self._drop(held[1])
                del self._by_event[e["id"]]
            if e.get("deleted") or e.get("ts") is None:
                continue
            type_id = e["typeId"]
            dead = type_id in OFF_TYPES or type_id in STOPPAGE_TYPES
            x, y = e.get("x"), e.get("y")
            if x is None or y is None:
                if not dead:
                    continue
                last = self.at_or_before(e["ts"])
                if last is None:
                    continue
                x, y = last.x, last.y
            sample = Sample(e["ts"], x, y, not dead, type_id in OFF_TYPES)
            self._by_event[e["id"]] = (seq, sample)
            self.add(sample.t, sample.x, sample.y, sample.live, sample.off)

    def at_or_before(self, t: float) -> Sample | None:
        i = bisect.bisect_right(self._samples, Sample(t, float("inf"), float("inf"), True, True))
        return self._samples[i - 1] if i else None

    def between(self, start: float, end: float) -> list[Sample]:
        """The samples from start to end, in time order."""
        i = bisect.bisect_left(self._samples, Sample(start, -1, -1, False, False))
        j = bisect.bisect_right(self._samples, Sample(end, float("inf"), float("inf"), True, True))
        return self._samples[i:j]

    def recent_json(self, now: float, seconds: float = 90.0) -> list[list]:
        """The last `seconds` of samples as [t, x, y, live, off] rows, for clients to draw the ball on the board."""
        return [[s.t, round(s.x, 2), round(s.y, 2), s.live, s.off] for s in self.between(now - seconds, now + seconds)]

    def position_at(self, t: float) -> tuple[float, float] | None:
        """The ball at t, on a straight line between the samples either side; None until t is covered."""
        if not self._samples or self._samples[-1].t < t:
            return None
        i = bisect.bisect_left(self._samples, Sample(t, -1, -1, False, False))
        after = self._samples[i]
        before = self._samples[i - 1] if i else None
        if before is None or after.t == before.t:
            return after.x, after.y
        share = (t - before.t) / (after.t - before.t)
        return before.x + (after.x - before.x) * share, before.y + (after.y - before.y) * share

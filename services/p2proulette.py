"""Football Roulette as P2P pots on live Opta events. Play money only.

Markets: where the next pass, shot or foul after betting closes happens. Players bet against each other into
one pot per round. No price is set.

A round starts when a player places a betslip with no round running on that market: the betting window is
the one that player picked (ROUND_SECONDS). Everyone in the room sees the countdown and can add a slip until
it closes. One round per market at a time; the next slip after it ends starts the next one.

Bets: a zone 1-36, or an outside bet (GROUPS): red/black, odd/even, home half/away half (x2), a third of the
pitch or a band along it (x3). Winners share the pot, less HOUSE_CUT, in proportion to stake x the roulette
multiplier, so a single zone pays most. A point on a line wins every zone it touches, and an outside bet wins
if any of those zones is in it. If nobody wins, every stake is returned.

The first qualifying event after betting closes settles the round, at its x,y. Live events can come late and
out of order, so the round waits EVENT_SETTLE_WAIT after the first candidate arrives and then takes the
earliest one. The end of the half before it voids the round, and so does the market's void time.
"""
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from services import opta
from services.roulette import CELLS, CHIPS, MAX_STAKE, RED, cells_at
from services.wallet import Wallets

ROUND_SECONDS = (5, 10, 30, 60)
# The result shows this long before the market takes a new round.
RESULT_SECONDS = 6
# After the first candidate event arrives, wait this long for an earlier one sent late (live Opta only).
EVENT_SETTLE_WAIT = {"tracking": 0, "events": 8}
# A next-shot round runs until the next shot. A half lasts under an hour, so a round still locked after this
# lost its period-end event (a feed outage or rebuild) and is voided rather than held for ever.
UNTIL_NEXT_BACKSTOP = 2 * 60 * 60
HOUSE_CUT = 0.0
STRAIGHT = 36
ZONES = range(1, CELLS + 1)
# Outside bets, as on a roulette table. With zone = column * 3 + row + 1, 1-18 is the home half and 19-36 the
# away half, the thirds are thirds of the pitch, and a "row" (a roulette column) is a band along the pitch:
# row1 the top channel (y 67-100), row2 the centre, row3 the bottom channel.
GROUPS = {
    "red": frozenset(RED), "black": frozenset(z for z in ZONES if z not in RED),
    "odd": frozenset(z for z in ZONES if z % 2), "even": frozenset(z for z in ZONES if not z % 2),
    "low": frozenset(range(1, 19)), "high": frozenset(range(19, 37)),
    "third1": frozenset(range(1, 13)), "third2": frozenset(range(13, 25)), "third3": frozenset(range(25, 37)),
    "row1": frozenset(z for z in ZONES if (z - 1) % 3 == 0), "row2": frozenset(z for z in ZONES if (z - 1) % 3 == 1),
    "row3": frozenset(z for z in ZONES if (z - 1) % 3 == 2),
}
OUTSIDE = {k: 3 if k.startswith(("third", "row")) else 2 for k in GROUPS}
MAX_SELECTIONS = 48
HISTORY = 12


def _in_play_positioned(event: dict) -> bool:
    return event["x"] is not None and event["team"] is not None and not event["deleted"] and event["periodId"] in (1, 2)


def _of_types(*type_ids: int) -> Callable[[dict], bool]:
    return lambda event: _in_play_positioned(event) and event["typeId"] in type_ids


@dataclass(frozen=True)
class Market:
    name: str
    label: str
    definition: str
    qualifies: Callable[[dict], bool]
    void_after: float | None  # match seconds after the close; None: until the next one


# Opta type ids from docs/opta_reference.md. Pass: 1 Pass, 2 Offside pass. Shot: 13 Miss, 14 Post, 15 Attempt
# saved, 16 Goal. Foul: 4, sent as a committed/won pair at the same absolute point.
MARKETS = {
    "next_pass": Market("next_pass", "Next pass", "Where the first pass after betting closes is played from.",
                        _of_types(1, 2), 120.0),
    "next_shot": Market("next_shot", "Next shot", "Where the first shot after betting closes is taken from.",
                        _of_types(*opta.SHOTS), None),
    "next_foul": Market("next_foul", "Next foul", "Where the first foul after betting closes happens.",
                        _of_types(opta.FOUL), 900.0),
}


class BetError(ValueError):
    pass


@dataclass
class Bet:
    player: str
    kind: str  # "zone" or a key of OUTSIDE
    zone: int | None
    amount: int

    @property
    def multiplier(self) -> int:
        return STRAIGHT if self.kind == "zone" else OUTSIDE[self.kind]

    def wins(self, zones: set[int]) -> bool:
        if self.kind == "zone":
            return self.zone in zones
        return any(z in GROUPS[self.kind] for z in zones)


@dataclass
class Round:
    id: str
    market: str
    seconds: int
    opens_at: float
    locks_at: float
    status: str = "open"  # open -> locked -> settled | void
    deadline: float | None = None  # match seconds, or real seconds when deadline_real
    deadline_real: bool = False
    clock_stopped: bool = False  # the match clock read None once; the next tick voids
    candidates: dict[int, dict] = field(default_factory=dict)  # qualifying events after the close
    first_candidate_at: float | None = None
    period_end_ts: float | None = None
    done_at: float | None = None  # settled or void: the result shows until done_at + RESULT_SECONDS
    bets: list[Bet] = field(default_factory=list)

    @property
    def pot(self) -> int:
        return sum(b.amount for b in self.bets)


class P2PRoulette:
    """Single-threaded: call only from the event loop."""

    def __init__(self, *, schedule: Callable[[float, Callable[[], None]], object], wallets: Wallets,
                 can_run: Callable[[], bool], match_time: Callable[[], float | None] = lambda: None,
                 now: Callable[[], float] = time.time, source: str = "events", room: str = "public"):
        self._schedule, self._wallets, self._can_run, self._now = schedule, wallets, can_run, now
        self._match_time = match_time
        self._source = source  # "events" for live Opta, "tracking" for the demo
        self.room = room
        self._rounds: dict[str, Round] = {}
        self._last: dict[str, dict] = {}
        self._history: list[dict] = []
        self._listeners: dict[str, set[Callable[[dict], None]]] = {}
        self._stopped = False

    # ---- rounds ----

    def start(self) -> None:
        self._tick()

    def stop(self) -> None:
        self._stopped = True

    def _tick(self) -> None:
        if self._stopped:
            return
        if self._step():
            self._broadcast()
        self._schedule(1.0, self._tick)

    def _step(self) -> bool:
        """Move every round on. Return True when players need a new state."""
        now, changed = self._now(), False
        running = self._can_run()
        for name, r in list(self._rounds.items()):
            if r.status in ("settled", "void"):
                if now >= r.done_at + RESULT_SECONDS:
                    del self._rounds[name]
                    changed = True
                continue
            if not running and not (r.status == "locked" and r.candidates):
                self._void(r, "the match is not in play")
                changed = True
            elif not running:
                # A qualifying event came before the whistle: let the settle rule decide, not the phase.
                changed = self._check(r, now) or changed
            elif r.status == "open" and now >= r.locks_at:
                self._lock(r, now)
                changed = True
            elif r.status == "locked":
                changed = self._check(r, now) or changed
        return changed

    def _lock(self, r: Round, now: float) -> None:
        r.status = "locked"
        market = MARKETS[r.market]
        if market.void_after is None:
            r.deadline, r.deadline_real = r.locks_at + UNTIL_NEXT_BACKSTOP, True
            return
        clock = self._match_time()
        # The void time is match time: checked each second, so a paused or faster demo voids after the same
        # stretch of play. With no match clock to hand, it falls back to real time.
        if clock is not None:
            r.deadline = clock - max(0.0, now - r.locks_at) + market.void_after
        else:
            r.deadline, r.deadline_real = r.locks_at + market.void_after, True

    def _check(self, r: Round, now: float) -> bool:
        if r.candidates and now >= r.first_candidate_at + EVENT_SETTLE_WAIT[self._source]:
            first = min(r.candidates.values(), key=lambda e: (e["ts"], e["id"]))
            if r.period_end_ts is not None and r.period_end_ts < first["ts"]:
                self._void(r, "the half ended first")
            else:
                self._settle(r, cells_at(first["x"], first["y"]), first)
            return True
        if r.period_end_ts is not None and not r.candidates:
            self._void(r, "the half ended first")
            return True
        if r.candidates:
            return False
        if r.deadline_real:
            if now >= r.deadline:
                self._void(r, "no qualifying event arrived")
                return True
            return False
        clock = self._match_time()
        if clock is None:
            # The half ended or the feed went quiet. Wait one tick first, so events already queued with the
            # period end can settle the round.
            if r.clock_stopped:
                self._void(r, "the match clock stopped")
                return True
            r.clock_stopped = True
            return False
        r.clock_stopped = False
        if clock >= r.deadline:
            self._void(r, "no qualifying event arrived")
            return True
        return False

    # ---- feed ----

    def on_events(self, events: list[dict]) -> None:
        """Collect qualifying events for the rounds whose betting has closed."""
        if not self._rounds:
            return
        # Lock any round due to close first, so an event just after the close is not missed. Read the batch
        # before the phase check: the feed thread can show the half as over before its own events arrive here.
        now = self._now()
        for r in self._rounds.values():
            if r.status == "open" and now >= r.locks_at:
                self._lock(r, now)
        timed = [e for e in events if e.get("ts") is not None]
        for name, r in self._rounds.items():
            if r.status != "locked":
                continue
            market = MARKETS[name]
            for e in timed:
                if e["ts"] <= r.locks_at:
                    continue
                if e.get("deleted"):
                    if r.candidates.pop(e["id"], None) is not None and not r.candidates:
                        r.first_candidate_at = None
                    continue
                if e["typeId"] == opta.PERIOD_END:
                    r.period_end_ts = e["ts"] if r.period_end_ts is None else min(r.period_end_ts, e["ts"])
                elif market.qualifies(e):
                    r.candidates[e["id"]] = e
                    if r.first_candidate_at is None:
                        r.first_candidate_at = now
        if self._step():
            self._broadcast()

    # ---- connections ----

    def connect(self, player_id: str, send: Callable[[dict], None]) -> None:
        self._listeners.setdefault(player_id, set()).add(send)
        send(self._state(player_id))

    def disconnect(self, player_id: str, send: Callable[[dict], None]) -> None:
        sends = self._listeners.get(player_id)
        if sends is not None:
            sends.discard(send)
            if not sends:
                del self._listeners[player_id]

    @property
    def viewers(self) -> int:
        return len(self._listeners)

    # ---- actions ----

    def handle(self, player_id: str, message: dict) -> None:
        # The client's tag for this betslip, echoed back so it clears the right slip, or keeps it on an error.
        ref = message.get("ref")
        ref = ref if isinstance(ref, str) and len(ref) <= 40 else None
        try:
            if message.get("type") != "br_place":
                raise BetError("unknown action")
            self._place(player_id, message)
        except BetError as exc:
            self._send(player_id, {"type": "br_error", "message": str(exc), "ref": ref})
            return
        self._send(player_id, {"type": "br_placed", "ref": ref, "market": message.get("market")})
        self._broadcast()

    def _place(self, player_id: str, message: dict) -> None:
        """A whole betslip: every selection goes in, or none does."""
        name = message.get("market")
        if not isinstance(name, str) or name not in MARKETS:
            raise BetError("pick a market")
        selections = message.get("selections")
        if not isinstance(selections, list) or not selections:
            raise BetError("add a selection to the betslip first")
        if len(selections) > MAX_SELECTIONS:
            raise BetError(f"at most {MAX_SELECTIONS} selections in one betslip")
        bets = [self._parse(player_id, s) for s in selections]
        total = sum(b.amount for b in bets)
        balance = self._wallets.balance(player_id)
        if total > balance:
            raise BetError("not enough balance")
        if not self._can_run():
            raise BetError("bets open when the match is in play")
        r = self._rounds.get(name)
        now = self._now()
        if r is None:
            seconds = message.get("seconds")
            if type(seconds) is not int or seconds not in ROUND_SECONDS:
                raise BetError("pick a betting window")
            r = self._rounds[name] = Round(uuid.uuid4().hex[:8], name, seconds, now, now + seconds)
        elif r.status != "open" or now >= r.locks_at:
            raise BetError(f"a {MARKETS[name].label.lower()} round is running; wait for it to finish")
        self._wallets.set(player_id, balance - total)
        r.bets.extend(bets)

    @staticmethod
    def _parse(player_id: str, s) -> Bet:
        if not isinstance(s, dict):
            raise BetError("that selection is not valid")
        kind, zone, amount = s.get("kind"), s.get("zone"), s.get("amount")
        if not isinstance(kind, str):
            raise BetError("pick a zone or an outside bet")
        if kind == "zone":
            if type(zone) is not int or not 1 <= zone <= CELLS:
                raise BetError(f"pick a zone from 1 to {CELLS}")
        elif kind in OUTSIDE:
            zone = None
        else:
            raise BetError("pick a zone or an outside bet")
        if type(amount) is not int or not 1 <= amount <= MAX_STAKE:
            raise BetError(f"enter a whole amount from 1 to {MAX_STAKE}")
        return Bet(player_id, kind, zone, amount)

    # ---- settlement ----

    def _settle(self, r: Round, zones: set[int], event: dict) -> None:
        winners = [b for b in r.bets if b.wins(zones)]
        payouts: dict[str, float] = {}
        if winners:
            weight = sum(b.amount * b.multiplier for b in winners)
            share = r.pot * (1 - HOUSE_CUT)
            for b in winners:
                payouts[b.player] = payouts.get(b.player, 0) + share * b.amount * b.multiplier / weight
            r.status = "settled"
            reason = None
        else:
            for b in r.bets:
                payouts[b.player] = payouts.get(b.player, 0) + b.amount
            r.status = "void"
            reason = "nobody called it, so every stake is returned"
        self._pay(payouts)
        self._record(r, {"zones": sorted(zones), "x": event["x"], "y": event["y"], "reason": reason,
                         "event": {k: event.get(k) for k in ("id", "name", "player", "team", "min", "sec")}}, payouts)

    def _void(self, r: Round, reason: str) -> None:
        payouts: dict[str, float] = {}
        for b in r.bets:
            payouts[b.player] = payouts.get(b.player, 0) + b.amount
        self._pay(payouts)
        r.status = "void"
        self._record(r, {"zones": [], "reason": reason}, payouts)

    def _pay(self, payouts: dict[str, float]) -> None:
        for player, amount in payouts.items():
            self._wallets.set(player, self._wallets.balance(player) + amount)

    def _record(self, r: Round, outcome: dict, payouts: dict[str, float]) -> None:
        r.done_at = self._now()
        stakes: dict[str, int] = {}
        for b in r.bets:
            stakes[b.player] = stakes.get(b.player, 0) + b.amount
        row = {"id": r.id, "market": r.market, "label": MARKETS[r.market].label, "status": r.status, "pot": r.pot,
               "players": len(stakes), **outcome, "payouts": {p: round(v, 2) for p, v in payouts.items()},
               "stakes": stakes}
        self._last[r.market] = row
        self._history = [row, *self._history][:HISTORY]

    def void_all(self, reason: str) -> None:
        """Refund every round not yet paid, e.g. when a demo jumps in time."""
        for r in self._rounds.values():
            if r.status in ("open", "locked"):
                self._void(r, reason)
        self._rounds.clear()
        self._broadcast()

    def active(self) -> bool:
        return any(r.status in ("open", "locked") for r in self._rounds.values())

    # ---- views ----

    @staticmethod
    def _personal(h: dict | None, player_id: str) -> dict | None:
        if h is None:
            return None
        return {**{k: v for k, v in h.items() if k not in ("payouts", "stakes")},
                "myStake": h["stakes"].get(player_id, 0), "myReturn": h["payouts"].get(player_id, 0)}

    def _round_json(self, r: Round, player_id: str) -> dict:
        by_zone: dict[int, int] = {}
        by_outside: dict[str, int] = {}
        for b in r.bets:
            if b.kind == "zone":
                by_zone[b.zone] = by_zone.get(b.zone, 0) + b.amount
            else:
                by_outside[b.kind] = by_outside.get(b.kind, 0) + b.amount
        return {"id": r.id, "market": r.market, "status": r.status, "seconds": r.seconds,
                "opensAt": r.opens_at, "locksAt": r.locks_at, "doneAt": r.done_at, "pot": r.pot,
                "players": len({b.player for b in r.bets}), "byZone": by_zone, "byOutside": by_outside,
                "mine": [{"kind": b.kind, "zone": b.zone, "amount": b.amount} for b in r.bets if b.player == player_id]}

    def _state(self, player_id: str) -> dict:
        return {"type": "br_state", "state": {
            "room": self.room,
            "markets": {name: {"label": m.label, "definition": m.definition, "voidAfter": m.void_after}
                        for name, m in MARKETS.items()},
            "rounds": {name: self._round_json(r, player_id) for name, r in self._rounds.items()},
            "last": {name: self._personal(h, player_id) for name, h in self._last.items()},
            "history": [self._personal(h, player_id) for h in self._history],
            "houseCut": HOUSE_CUT, "multipliers": {"zone": STRAIGHT, **OUTSIDE},
            "groups": {k: sorted(v) for k, v in GROUPS.items()},
            "roundSeconds": ROUND_SECONDS, "resultSeconds": RESULT_SECONDS, "chipValues": CHIPS,
            "maxStake": MAX_STAKE, "maxSelections": MAX_SELECTIONS, "viewers": self.viewers,
            "balance": round(self._wallets.balance(player_id), 2), "serverTime": self._now()}}

    def _send(self, player_id: str, message: dict) -> None:
        for send in list(self._listeners.get(player_id, ())):
            send(message)

    def _broadcast(self) -> None:
        for player_id in list(self._listeners):
            self._send(player_id, self._state(player_id))

"""P2P penalty pick: where in the goal will the penalty be scored? Play money only.

A penalty award (an Opta foul, outcome 1, with the penalty qualifier) opens one pot in each room. Players
have PICK_SECONDS to stake on one of nine goal zones: three columns (left, centre, right, from the
taker's view) by three rows (top, middle, bottom). Only a goal counts. A save, a miss or the woodwork
means nobody wins, and every stake is returned. Winners share the pot, less HOUSE_CUT, in proportion to
their stakes. A kick on a zone boundary wins every zone it touches.

The zone comes from the kick's Opta goal-mouth position (q102 across the goal, q103 height), with the
frame that static/moments.js draws. The demo data has no goal-mouth position, so there the column comes
from the ball track where it crosses the goal line, and all three rows of that column win.
"""
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from services import opta
from services.balltrack import BallTrack
from services.roulette import MAX_STAKE
from services.wallet import Wallets

PICK_SECONDS = 30
KICK_WAIT = 240  # seconds after the award with no kick: the pot is void
SPOT_WAIT = 90  # seconds after a goal with no goal-mouth position (live): the pot is void
CROSS_WAIT = 10  # seconds after a demo goal for the ball track to cross the line
STALE_AFTER = 120  # an award stamped this long ago is a re-push or a catch-up, not a new penalty
HOUSE_CUT = 0.0
HISTORY = 10
ZONES = 9
COLS = ("left", "centre", "right")
ROWS = ("Top", "Middle", "Bottom")
# Opta goal-mouth frame, as in static/moments.js: y 55 is the left post and 45 the right post from the
# taker's view (y grows toward the attacker's left), z 36 is the bar.
GM_LEFT, GM_RIGHT, GM_BAR = 55.0, 45.0, 36.0
# The posts on the pitch y axis (pitch percent, attacker's frame): a 7.32 m goal on a 68 m wide pitch.
POST_HALF = 7.32 / 2 / 68 * 100


class PenaltyError(ValueError):
    pass


def zone_name(zone: int) -> str:
    return f"{ROWS[zone // 3]} {COLS[zone % 3]}"


def _bands(share: float) -> set[int]:
    """The thirds (0, 1, 2) a share from 0 to 1 falls in; on a boundary, both thirds."""
    scaled = min(1.0, max(0.0, share)) * 3
    edge = round(scaled)
    if abs(scaled - edge) < 1e-9 and 0 < edge < 3:  # on a line between two thirds, to float precision
        return {edge - 1, edge}
    return {min(2, int(scaled))}


def zones_from_goal_mouth(gy: float, gz: float) -> set[int]:
    """The goal zones a kick at goal-mouth (gy, gz) wins. A goal just outside the drawn frame is held to
    the nearest zone, as the frame is an estimate."""
    cols = _bands((GM_LEFT - gy) / (GM_LEFT - GM_RIGHT))
    rows = _bands(1 - gz / GM_BAR)
    return {r * 3 + c for r in rows for c in cols}


def column_from_pitch_y(y: float) -> set[int] | None:
    """The columns for a ball crossing the goal line at pitch y (attacker's frame); None if wide."""
    if abs(y - 50) > POST_HALF:
        return None
    left, right = 50 + POST_HALF, 50 - POST_HALF
    return _bands((left - y) / (left - right))


@dataclass
class Stake:
    player: str
    zone: int
    amount: int


@dataclass
class Pot:
    id: str
    award_id: int
    team: str
    award_ts: float
    opened_at: float
    closes_at: float
    void_at: float
    minute: int
    second: int
    status: str = "open"  # open -> locked -> settled | void
    shot: dict | None = None
    shot_seen_at: float | None = None
    stakes: list[Stake] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(s.amount for s in self.stakes)


class PenaltyPot:
    """Single-threaded: call only from the event loop."""

    def __init__(self, *, schedule: Callable[[float, Callable[[], None]], object], wallets: Wallets, track: BallTrack,
                 phase: Callable[[], str], now: Callable[[], float] = time.time, room: str = "public"):
        self._schedule, self._wallets, self._track, self._phase, self._now = schedule, wallets, track, phase, now
        self.room = room
        self._pot: Pot | None = None
        self._seen: set[int] = set()
        self._shots: dict[int, dict] = {}  # this pot's candidate kicks, by event id (newest version)
        self._last: dict | None = None
        self._history: list[dict] = []
        self._listeners: dict[str, set[Callable[[dict], None]]] = {}
        self._stopped = False

    # ---- timer ----

    def start(self) -> None:
        self._tick()

    def stop(self) -> None:
        self._stopped = True

    def _tick(self) -> None:
        if self._stopped:
            return
        if self._evaluate():
            self._broadcast()
        self._schedule(1.0, self._tick)

    # ---- feed ----

    def on_events(self, events: list[dict]) -> None:
        changed = False
        for e in events:
            if self._is_award(e):
                changed = self._open(e) or changed
            elif self._pot and self._pot.status in ("open", "locked"):
                changed = self._watch(e) or changed
        if self._evaluate() or changed:
            self._broadcast()

    def on_phase(self, phase: str) -> None:
        if self._pot and self._pot.status in ("open", "locked") and phase != "live":
            self._void("the match stopped before the penalty was scored")
            self._broadcast()

    def _is_award(self, e: dict) -> bool:
        return (e["typeId"] == opta.FOUL and e.get("outcome") == 1 and bool(e.get("penalty"))
                and e.get("team") is not None and not e.get("deleted"))

    def _open(self, award: dict) -> bool:
        if award["id"] in self._seen:
            return False
        self._seen.add(award["id"])
        now = self._now()
        ts = award.get("ts")
        if self._phase() != "live" or ts is None or now - ts > STALE_AFTER:
            return False
        if self._pot and self._pot.status in ("open", "locked"):
            return False
        # Picks close 30 s after the award itself, not after it reached us: a late feed must not
        # leave picks open after the kick is on TV.
        closes_at = min(now + PICK_SECONDS, ts + PICK_SECONDS)
        if closes_at <= now:
            return False
        self._pot = Pot(uuid.uuid4().hex[:8], award["id"], award["team"], ts, now, closes_at,
                        now + KICK_WAIT, award.get("min") or 0, award.get("sec") or 0)
        self._shots = {}
        return True

    def _watch(self, e: dict) -> bool:
        pot = self._pot
        if e["id"] == pot.award_id and e.get("deleted"):
            self._void("the penalty award was taken back")
            return True
        if e["typeId"] == opta.PERIOD_END and not e.get("deleted"):
            self._void("the half ended before the penalty was taken")
            return True
        if e["id"] in self._shots:
            if e.get("deleted"):
                del self._shots[e["id"]]
                if pot.shot and pot.shot["id"] == e["id"]:
                    pot.shot, pot.shot_seen_at = None, None
                    self._void("the goal was taken back")
                return True
            self._shots[e["id"]] = e
            if pot.shot and pot.shot["id"] == e["id"]:
                pot.shot = e  # a re-push can add the goal-mouth position
            return True
        if (e["typeId"] in opta.SHOTS and e.get("penalty") and e.get("team") == pot.team and not e.get("deleted")
                and e.get("ts") is not None and e["ts"] >= pot.award_ts):
            self._shots[e["id"]] = e
            return True
        return False

    # ---- settlement ----

    def _evaluate(self) -> bool:
        pot = self._pot
        if pot is None or pot.status not in ("open", "locked"):
            return False
        now, changed = self._now(), False
        if pot.status == "open" and now >= pot.closes_at:
            pot.status, changed = "locked", True
        if pot.shot is None and self._shots:
            pot.shot = min(self._shots.values(), key=lambda e: (e["ts"], e["id"]))
            pot.shot_seen_at = now
            if pot.status == "open":
                pot.status = "locked"  # the kick is in: picks close at once
            changed = True
        shot = pot.shot
        if shot is None:
            if now >= pot.void_at:
                self._void("no penalty kick came from the feed")
                return True
            return changed
        if shot["typeId"] != opta.GOAL:
            what = "saved" if shot["typeId"] == opta.ATTEMPT_SAVED else "off the woodwork" if shot["typeId"] == opta.POST else "missed"
            self._settle(set(), {"scored": False, "kick": what, "player": shot.get("player")},
                         f"the penalty was {what}, so every stake is returned")
            return True
        gy, gz = shot.get("goalMouthY"), shot.get("goalMouthZ")
        if gy is not None and gz is not None:
            self._settle(zones_from_goal_mouth(gy, gz), {"scored": True, "player": shot.get("player"), "gy": gy, "gz": gz})
            return True
        if self._track.source == "tracking":
            columns = self._crossing(shot)
            if columns is None and now < pot.shot_seen_at + CROSS_WAIT:
                return changed
            if columns is None:
                self._void("the ball track did not show where it went in")
                return True
            zones = {r * 3 + c for r in range(3) for c in columns}
            self._settle(zones, {"scored": True, "player": shot.get("player"), "columnOnly": True})
            return True
        if now >= pot.shot_seen_at + SPOT_WAIT:
            self._void("scored, but the feed did not send where in the goal")
            return True
        return changed

    def _crossing(self, shot: dict) -> set[int] | None:
        """The demo: the columns where the tracked ball crosses the goal line after the kick. The home team
        attacks the goal at x 100, the away team the goal at x 0 (the frame of match_state)."""
        home = self._pot.team == "home"
        before = None
        for s in self._track.between(shot["ts"] - 2, shot["ts"] + 30):
            past = s.x >= 100 if home else s.x <= 0
            if past:
                y = s.y
                if before is not None and before.x != s.x:
                    line = 100.0 if home else 0.0
                    share = (line - before.x) / (s.x - before.x)
                    y = before.y + (s.y - before.y) * share
                return column_from_pitch_y(y if home else 100 - y)
            before = s
        return None

    def _settle(self, zones: set[int], outcome: dict, no_win: str = "nobody picked that zone, so every stake is returned") -> None:
        pot = self._pot
        winners = [s for s in pot.stakes if s.zone in zones]
        payouts: dict[str, float] = {}
        if winners:
            weight = sum(s.amount for s in winners)
            share = pot.total * (1 - HOUSE_CUT)
            for s in winners:
                payouts[s.player] = payouts.get(s.player, 0) + share * s.amount / weight
            pot.status, reason = "settled", None
        else:
            for s in pot.stakes:
                payouts[s.player] = payouts.get(s.player, 0) + s.amount
            pot.status, reason = ("void", no_win) if pot.stakes else ("settled", None)
        self._pay(payouts)
        self._record({"zones": sorted(zones), "names": [zone_name(z) for z in sorted(zones)], "reason": reason, **outcome},
                     payouts)

    def _void(self, reason: str) -> None:
        pot = self._pot
        payouts: dict[str, float] = {}
        for s in pot.stakes:
            payouts[s.player] = payouts.get(s.player, 0) + s.amount
        self._pay(payouts)
        pot.status = "void"
        self._record({"zones": [], "names": [], "reason": reason}, payouts)

    def _pay(self, payouts: dict[str, float]) -> None:
        for player, amount in payouts.items():
            self._wallets.set(player, self._wallets.balance(player) + amount)

    def _record(self, outcome: dict, payouts: dict[str, float]) -> None:
        pot = self._pot
        stakes: dict[str, int] = {}
        for s in pot.stakes:
            stakes[s.player] = stakes.get(s.player, 0) + s.amount
        row = {"id": pot.id, "status": pot.status, "pot": pot.total, "players": len(stakes), "team": pot.team,
               "min": pot.minute, "sec": pot.second, **outcome,
               "payouts": {p: round(v, 2) for p, v in payouts.items()}, "stakes": stakes}
        self._last = row
        if pot.stakes:
            self._history = [row, *self._history][:HISTORY]

    def void_all(self, reason: str) -> None:
        """Refund a pot not yet paid, e.g. when a demo jumps in time. The demo replays the same award ids."""
        if self._pot and self._pot.status in ("open", "locked"):
            self._void(reason)
        self._seen.clear()
        self._broadcast()

    def active(self) -> bool:
        return bool(self._pot and self._pot.status in ("open", "locked") and self._pot.stakes)

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
        try:
            if message.get("type") != "pen_bet":
                raise PenaltyError("unknown action")
            self._bet(player_id, message)
        except PenaltyError as exc:
            self._send(player_id, {"type": "pen_error", "message": str(exc)})
            return
        self._broadcast()

    def _bet(self, player_id: str, message: dict) -> None:
        pot = self._pot
        if pot is None or pot.status != "open" or self._now() >= pot.closes_at:
            raise PenaltyError("picks are closed for this penalty")
        zone, amount = message.get("zone"), message.get("amount")
        if type(zone) is not int or not 0 <= zone < ZONES:
            raise PenaltyError("pick one of the nine goal zones")
        if type(amount) is not int or not 1 <= amount <= MAX_STAKE:
            raise PenaltyError(f"enter a whole amount from 1 to {MAX_STAKE}")
        balance = self._wallets.balance(player_id)
        if amount > balance:
            raise PenaltyError("not enough balance")
        self._wallets.set(player_id, balance - amount)
        pot.stakes.append(Stake(player_id, zone, amount))

    # ---- views ----

    def _personal(self, h: dict | None, player_id: str) -> dict | None:
        if h is None:
            return None
        return {**{k: v for k, v in h.items() if k not in ("payouts", "stakes")},
                "myStake": h["stakes"].get(player_id, 0), "myReturn": h["payouts"].get(player_id, 0)}

    def _state(self, player_id: str) -> dict:
        pot = self._pot
        pot_json = None
        if pot:
            by_zone: dict[int, int] = {}
            for s in pot.stakes:
                by_zone[s.zone] = by_zone.get(s.zone, 0) + s.amount
            pot_json = {"id": pot.id, "status": pot.status, "team": pot.team, "openedAt": pot.opened_at,
                        "closesAt": pot.closes_at, "min": pot.minute, "sec": pot.second, "pot": pot.total,
                        "players": len({s.player for s in pot.stakes}), "byZone": by_zone,
                        "kickIn": pot.shot is not None,
                        "mine": [{"zone": s.zone, "amount": s.amount} for s in pot.stakes if s.player == player_id]}
        return {"type": "pen_state", "state": {
            "room": self.room, "pot": pot_json, "last": self._personal(self._last, player_id),
            "history": [self._personal(h, player_id) for h in self._history],
            "pickSeconds": PICK_SECONDS, "houseCut": HOUSE_CUT, "maxStake": MAX_STAKE,
            "balance": round(self._wallets.balance(player_id), 2), "serverTime": self._now()}}

    def _send(self, player_id: str, message: dict) -> None:
        for send in list(self._listeners.get(player_id, ())):
            send(message)

    def _broadcast(self) -> None:
        for player_id in list(self._listeners):
            self._send(player_id, self._state(player_id))

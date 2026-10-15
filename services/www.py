"""Who Where When: a pre-match P2P pot on the first goal of the match. Play money only.

Before kick-off, pick the scorer, the 12x3 grid zone he scores from (the roulette numbering, home attacks
right) and the 15-minute band. Every stake goes into one pot. No price is set.
- Who is a gate: the wrong scorer gets no share.
- Where: 1 if the goal is in the picked zone (a goal on a line is in every zone it touches), NEAR_SCORE if
  the picked zone is next to one of those zones (8 around it), else 0.
- When: 1 for the goal's band, NEAR_SCORE for the band next to it, else 0.
Weight = stake x where x when. Winners share the pot, less HOUSE_CUT, by weight, so the exact zone and band
pay most and a neighbouring zone or band pays a smaller share. If nobody has a weight above 0, every stake is
returned.

The pot settles HOLD_SECONDS after the first goal, so a goal that VAR takes away (a deleted event) does
not pay; the next goal that stands is used instead. A goal with no position gives every pick a where
score of 0, so it refunds. No goal before full time, or a feed that stops, also refunds.

Who is the player the feed names on the goal event, with that event's team. For an own goal that is the
player who put it in his own net, as Opta credits it. A goal deleted after the pot has paid cannot be taken
back: the pot is closed. The hold is there so that VAR has time to act first.
"""
import time
from collections.abc import Callable
from dataclasses import dataclass

from services.roulette import CELLS, COLS, MAX_STAKE, ROWS, cells_at
from services.wallet import Wallets

HOLD_SECONDS = 120
# A neighbouring zone or band still gets a share, smaller than the exact one.
NEAR_SCORE = 0.3
HOUSE_CUT = 0.0
BANDS = ["0-14", "15-29", "30-44", "45-59", "60-74", "75-89", "90+"]
GOAL = 16
HISTORY = 10


class WwwError(ValueError):
    pass


@dataclass
class Pick:
    player: str
    scorer: str
    team: str
    cell: int
    band: int
    amount: int


def band_of(minute: int) -> int:
    """The band of a match-clock minute (Opta's second-half clock starts at 45:00)."""
    return min(minute // 15, len(BANDS) - 1)


def neighbours(cell: int) -> set[int]:
    """The up to 8 zones round a zone on the 12x3 grid."""
    col, row = (cell - 1) // ROWS, (cell - 1) % ROWS
    return {c * ROWS + r + 1 for c in range(col - 1, col + 2) for r in range(row - 1, row + 2)
            if 0 <= c < COLS and 0 <= r < ROWS and (c, r) != (col, row)}


def where_score(cell: int, goal: dict) -> float:
    if goal.get("x") is None or goal.get("y") is None:
        return 0.0
    zones = cells_at(goal["x"], goal["y"])
    if cell in zones:
        return 1.0
    return NEAR_SCORE if any(cell in neighbours(z) for z in zones) else 0.0


def when_score(band: int, goal: dict) -> float:
    gap = abs(band - band_of(goal["min"]))
    return 1.0 if gap == 0 else NEAR_SCORE if gap == 1 else 0.0


def score(pick: Pick, goal: dict) -> float:
    """The pick's share factor, from 0 to 1. The wrong scorer is 0."""
    if pick.scorer != goal.get("player") or pick.team != goal.get("team"):
        return 0.0
    return where_score(pick.cell, goal) * when_score(pick.band, goal)


class WhoWhereWhen:
    """Single-threaded: call only from the event loop."""

    def __init__(self, *, schedule: Callable[[float, Callable[[], None]], object], wallets: Wallets,
                 phase: Callable[[], str], squads: Callable[[], dict[str, set[str]]], now: Callable[[], float] = time.time):
        self._schedule, self._wallets, self._phase, self._squads, self._now = schedule, wallets, phase, squads, now
        self._picks: list[Pick] = []
        self._goals: dict[int, dict] = {}  # standing goals by event id
        self._holding: dict | None = None  # the goal the pot waits on, with "heldAt"
        self._result: dict | None = None
        self._history: dict[str, list[dict]] = {}
        self._listeners: dict[str, set[Callable[[dict], None]]] = {}
        self._seen_phase: str | None = None

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
            if message.get("type") != "www_bet":
                raise WwwError("unknown action")
            self._bet(player_id, message)
        except WwwError as exc:
            self._send(player_id, {"type": "www_error", "message": str(exc)})
            return
        self._broadcast()

    def _bet(self, player_id: str, m: dict) -> None:
        if self._phase() != "pre" or self._result is not None:
            raise WwwError("Who Where When closes at kick-off")
        scorer, team, cell, band, amount = m.get("scorer"), m.get("team"), m.get("cell"), m.get("band"), m.get("amount")
        if team not in ("home", "away") or not isinstance(scorer, str) or scorer not in self._squads().get(team, set()):
            raise WwwError("pick a scorer from the line-ups")
        if type(cell) is not int or not 1 <= cell <= CELLS:
            raise WwwError(f"pick a zone from 1 to {CELLS}")
        if type(band) is not int or not 0 <= band < len(BANDS):
            raise WwwError("pick a minute band")
        if type(amount) is not int or not 1 <= amount <= MAX_STAKE:
            raise WwwError(f"enter a whole amount from 1 to {MAX_STAKE}")
        balance = self._wallets.balance(player_id)
        if amount > balance:
            raise WwwError("not enough balance")
        self._wallets.set(player_id, balance - amount)
        self._picks.append(Pick(player_id, scorer, team, cell, band, amount))

    # ---- settlement ----

    def on_events(self, events: list[dict]) -> None:
        for e in events:
            if e.get("deleted"):
                self._goals.pop(e["id"], None)
            elif e["typeId"] == GOAL:
                self._goals[e["id"]] = e
        if self._result is not None or not self._picks:
            return
        first = self._first_goal()
        if first is None:
            if self._holding is None:
                return
            self._holding = None
        elif self._holding is None or self._holding["id"] != first["id"]:
            # A new first goal, or VAR took the old one away: wait the full hold on this one.
            self._holding = {**first, "heldAt": self._now()}
            held = self._holding
            self._schedule(HOLD_SECONDS, lambda: self._release(held))
        else:
            return
        self._broadcast()

    def on_phase(self, phase: str) -> None:
        """The phase changed. Kick-off closes the picks. At full time, or when the feed stops, with no goal
        to wait on, every stake goes back."""
        if phase != self._seen_phase:
            self._seen_phase = phase
            self._broadcast()
        if self._result is not None or not self._picks or phase not in ("ft", "ended") or self._holding:
            return
        self._refund("no goal before full time" if phase == "ft" else "the feed stopped before a goal")

    def _first_goal(self) -> dict | None:
        if not self._goals:
            return None
        return min(self._goals.values(), key=lambda g: (g.get("periodId") or 0, g["min"], g["sec"], g["id"]))

    def _release(self, held: dict) -> None:
        if self._result is not None or self._holding is not held:
            return
        goal = self._first_goal()
        if goal is None or goal["id"] != held["id"]:
            return
        self._settle(goal)

    def _settle(self, goal: dict) -> None:
        scored = [(p, score(p, goal)) for p in self._picks]
        winners = [(p, s) for p, s in scored if s > 0]
        pot = sum(p.amount for p in self._picks)
        payouts: dict[str, float] = {}
        if winners:
            weight = sum(p.amount * s for p, s in winners)
            for p, s in winners:
                payouts[p.player] = payouts.get(p.player, 0) + pot * (1 - HOUSE_CUT) * p.amount * s / weight
            reason = None
        else:
            for p in self._picks:
                payouts[p.player] = payouts.get(p.player, 0) + p.amount
            reason = "nobody was close enough, so every stake is returned"
        zones = sorted(cells_at(goal["x"], goal["y"])) if goal.get("x") is not None and goal.get("y") is not None else []
        outcome = {"status": "settled" if winners else "void", "reason": reason, "scorer": goal.get("player"),
                   "team": goal.get("team"), "cells": zones, "band": band_of(goal["min"]), "minute": goal["min"] + 1}
        self._finish(outcome, payouts, {id(p): s for p, s in scored})

    def _refund(self, reason: str) -> None:
        payouts: dict[str, float] = {}
        for p in self._picks:
            payouts[p.player] = payouts.get(p.player, 0) + p.amount
        self._finish({"status": "void", "reason": reason}, payouts, {})

    def _finish(self, outcome: dict, payouts: dict[str, float], scores: dict[int, float]) -> None:
        pot = sum(p.amount for p in self._picks)
        for player, amount in payouts.items():
            self._wallets.set(player, self._wallets.balance(player) + amount)
        self._result = {**outcome, "pot": pot, "players": len({p.player for p in self._picks})}
        for player in {p.player for p in self._picks}:
            mine = [p for p in self._picks if p.player == player]
            rows = self._history.setdefault(player, [])
            rows.insert(0, {**self._result, "picks": [{**self._pick_json(p), "score": round(scores.get(id(p), 0.0), 3)} for p in mine],
                            "stake": sum(p.amount for p in mine), "payout": round(payouts.get(player, 0.0), 2)})
            del rows[HISTORY:]
        self._holding = None
        self._broadcast()

    def void_all(self, reason: str) -> None:
        """Refund an open pot and start a new one, e.g. when a demo jumps in time and the match replays."""
        if self._result is None and self._picks:
            self._refund(reason)
        self._picks, self._goals, self._holding, self._result = [], {}, None, None
        self._broadcast()

    def active(self) -> bool:
        return self._result is None and bool(self._picks)

    # ---- views ----

    @staticmethod
    def _pick_json(p: Pick) -> dict:
        return {"scorer": p.scorer, "team": p.team, "cell": p.cell, "band": p.band, "amount": p.amount}

    def _state(self, player_id: str) -> dict:
        phase = self._phase()
        status = ("settled" if self._result else "holding" if self._holding
                  else "open" if phase == "pre" else "waiting")
        by_cell: dict[int, int] = {}
        by_scorer: dict[str, int] = {}
        for p in self._picks:
            by_cell[p.cell] = by_cell.get(p.cell, 0) + p.amount
            key = f"{p.team}:{p.scorer}"
            by_scorer[key] = by_scorer.get(key, 0) + p.amount
        return {"type": "www_state", "state": {
            "status": status, "pot": sum(p.amount for p in self._picks), "players": len({p.player for p in self._picks}),
            # Everyone sees where the money is, without names: the crowd is what sets the share.
            "byCell": by_cell, "byScorer": by_scorer,
            "totals": self._totals(),
            "mine": [self._pick_json(p) for p in self._picks if p.player == player_id],
            "holding": {"scorer": self._holding.get("player"), "minute": self._holding["min"] + 1,
                        "paysAt": self._holding["heldAt"] + HOLD_SECONDS} if self._holding else None,
            "result": self._result, "history": self._history.get(player_id, []), "bands": BANDS,
            "rules": {"nearScore": NEAR_SCORE, "holdSeconds": HOLD_SECONDS, "houseCut": HOUSE_CUT},
            "maxStake": MAX_STAKE, "balance": round(self._wallets.balance(player_id), 2), "serverTime": self._now()}}

    def _totals(self) -> list[dict]:
        """Stake on each distinct pick (scorer, zone, band), over the whole pot."""
        totals: dict[tuple, int] = {}
        for p in self._picks:
            key = (p.team, p.scorer, p.cell, p.band)
            totals[key] = totals.get(key, 0) + p.amount
        return [{"team": t, "scorer": s, "cell": c, "band": b, "amount": a} for (t, s, c, b), a in totals.items()]

    def _send(self, player_id: str, message: dict) -> None:
        for send in list(self._listeners.get(player_id, ())):
            send(message)

    def _broadcast(self) -> None:
        for player_id in list(self._listeners):
            self._send(player_id, self._state(player_id))

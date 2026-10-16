"""Live odds that move with the play: a price for each zone and outside bet, from where the ball is now and
which team has it.

The roulette's Odds system (services/p2proulette.py) takes these prices: a bet locks the price shown when it is
placed. The P2P system ignores them and pays from the pot.

Source: the pricing table from tools/build_odds.py (data/pricing/demo_<slug>.json, version 2), trained on DFL
matches. Its conditional rows are in the possessor frame: the team on the ball attacks right. The ball track,
the live Opta positions and the board are in the home-attacks-right frame, so for an away possessor the ball is
mirrored to look up the row, and the row's cells are mirrored back. The table stores winnings-to-1
(tools/build_pricing.py odds_from); the message sends decimal odds, the same scale as the pot's "pays now" price.

Ball and possessor: the demo has a 5 Hz ball track that knows who has the ball, and prices from it at once. A
live match has only Opta events, so the newest on-ball event (ON_BALL) gives the position and the team. Duels that
come in pairs, one per team (aerials), are left out: they do not say who has the ball. A pass is read at its end
point: the receiver plays next, or the other team when it failed. Every client sees the same events, so the price
must use all they show. When the ball goes dead (DEAD_BALL: a goal, out of play, a corner, a foul, an offside, a shot
wide) the
next pass is the restart, from a spot everyone knows, so it has no fair price: the live odds are suspended until an
on-ball event after it.

Age (live): an Opta event reaches us seconds after the play, and more play has happened by the time a bet lands.
A price read from fresh information would be too sharp, so a live price uses the table for information that old
(byLag, tools/build_odds.py), rounded up with LIVE_AGE_MARGIN, and the channel re-prices it each second as the
newest event ages. With nothing newer than LIVE_MAX_AGE the live odds are suspended. Opta gives event positions,
not the tracked ball the table was built on, so live prices also carry LIVE_EXTRA_MARGIN on top of the table's.
"""
import json
import math
import time
from collections.abc import Callable
from pathlib import Path

from services import opta
from services.p2proulette import GROUPS
from services.roulette import CELLS, cells_at, mirror

# Opta types where the acting team has the ball: pass, offside pass, take on, tackle (wins it), interception,
# keeper claim, the shots, ball recovery.
ON_BALL = frozenset({1, 2, 3, 7, 8, 11, 49} | opta.SHOTS)
# Opta types after which the ball is dead and the next pass is a restart: foul, out, corner awarded, offside (the
# pass and the defender's "provoked" event), miss, goal.
DEAD_BALL = frozenset({opta.FOUL, opta.OUT, opta.CORNER, opta.OFFSIDE_PASS, opta.OFFSIDE_PROVOKED, opta.MISS,
                       opta.GOAL})
# The markets this module prices. Next foul has none.
MARKETS = ("next_pass", "next_shot")
PASSES = frozenset({1, 2})
# A bet may quote the price just replaced for this long after the new one went out: the click and the next odds
# message can cross. Only that one price, so a client cannot pick the best of several old ones.
QUOTE_GRACE = 1.0
# A live price must hold until the bet lands: the next re-price (1 s) plus the quote grace.
LIVE_AGE_MARGIN = 2.0
LIVE_MAX_AGE = 60.0
# Extra overround on live prices, set by judgement to stay safe. On one recorded live match (tests/fixtures), a
# stake on every zone at every live price returned about 0.95 per unit without it and about 0.83 with it.
LIVE_EXTRA_MARGIN = 0.15


def outside_price(zones: list[float], members) -> float:
    """Decimal odds for an outside bet: one over the summed chances of its zones, at least 1.01."""
    return round(max(1.01, 1 / sum(1 / zones[z - 1] for z in members)), 2)


class LiveOdds:
    def __init__(self, path: str | Path, now: Callable[[], float] = time.time, live: bool = False):
        table = json.loads(Path(path).read_text())
        if table.get("version") != 2:
            raise ValueError(f"{path}: expected a version 2 pricing table")
        markets = table["markets"]
        self._by_ball_cell = {int(k): v for k, v in markets["next_pass"]["conditional"]["byBallCell"].items()}
        self._shot = markets["next_shot"]["conditional"]["any"]
        by_lag = markets["next_pass"]["conditional"].get("byLag", {})
        self._lags = sorted((int(lag), {int(k): v for k, v in rows.items()}) for lag, rows in by_lag.items())
        if live and not self._lags:
            raise ValueError(f"{path}: no byLag tables for live prices; rebuild it with tools/build_odds.py")
        self._now = now
        self._live: tuple[float, float, str] | None = None  # live: x, y, possessor of the newest on-ball event
        self.last: dict | None = None
        self._current: dict | None = None  # {"zones": ..., "outside": ...} now showing
        self._previous: tuple[float, dict] | None = None  # (replaced at, the price it replaced)
        self._live_ts: float | None = None  # the newest on-ball event used, live only

    def clear(self, reason: str = "reset") -> dict:
        """Drop the odds until the next sample: after a jump in time ("reset"), a dead ball ("dead"), or a feed with
        no play for LIVE_MAX_AGE ("stale")."""
        self.last, self._live_ts, self._live = None, None, None
        self._current, self._previous = None, None
        return {"type": "odds", "markets": None, "outside": None, "reason": reason}

    def quotes(self) -> list[dict]:
        """The prices a bet may take, newest first: the current one, and the one it replaced while that is
        inside QUOTE_GRACE. Each is {"zones": {market: [36]}, "outside": {market: {kind: price}}}."""
        out = [self._current] if self._current else []
        if self._previous and self._now() - self._previous[0] <= QUOTE_GRACE:
            out.append(self._previous[1])
        return out

    def _rows(self, age: float) -> dict[int, list[float]]:
        """The next-pass rows for information `age` seconds old: the first table at least that old."""
        for lag, rows in self._lags:
            if lag >= age:
                return rows
        return self._lags[-1][1] if self._lags else self._by_ball_cell

    def update(self, x: float, y: float, possession: str | None, age: float = 0.0,
               source_ts: float | None = None) -> dict | None:
        """The odds message for this ball sample if the odds changed, else None. With no known possessor the
        last odds stand: the table has no row for a loose ball."""
        if possession not in ("home", "away"):
            return None
        away = possession == "away"
        bx, by = (100 - x, 100 - y) if away else (x, y)
        ball_cell = min(cells_at(bx, by))  # the first cell, as tools/build_odds.py keys its rows

        def price(to_one: float) -> float:
            decimal = to_one + 1
            if age > 0:  # live: the extra margin, rounded down so rounding never favours the player
                decimal = max(1.01, math.floor(decimal / (1 + LIVE_EXTRA_MARGIN) * 10) / 10)
            return round(decimal, 2)

        def board(row: list[float]) -> list[float]:
            """Row of to-1 odds by possessor-frame cell -> decimal odds by board cell (cell n at index n - 1)."""
            out = [0.0] * CELLS
            for i, to_one in enumerate(row):
                out[(mirror(i + 1) if away else i + 1) - 1] = price(to_one)
            return out

        rows = self._by_ball_cell if age <= 0 else self._rows(age)
        zones = {"next_pass": board(rows[ball_cell]), "next_shot": board(self._shot)}
        outside = {name: {kind: outside_price(row, members) for kind, members in GROUPS.items()}
                   for name, row in zones.items()}
        message = {"type": "odds", "possession": possession, "markets": zones, "outside": outside}
        if message == self.last:
            if self._current:
                self._current["sourceTs"] = source_ts  # the same price, now read from a newer event
            return None
        self.last = message
        if self._current:
            self._previous = (self._now(), self._current)
        # sourceTs: the time of the play the price was read from (live), so a bet knows what its price knew.
        self._current = {"zones": zones, "outside": outside, "sourceTs": source_ts}
        return message

    def update_from_events(self, events: list[dict]) -> dict | None:
        """Live: take the newest on-ball or dead-ball event in the batch, if it is newer than the last one used.
        A dead ball suspends the odds; an on-ball event prices from where the ball is after it."""
        newest = None
        for e in events:
            if (e.get("typeId") in ON_BALL | DEAD_BALL and e.get("team") in ("home", "away")
                    and e.get("ts") is not None and not e.get("deleted") and e.get("periodId") in (1, 2)
                    and (e.get("x") is not None or e["typeId"] in DEAD_BALL)):
                # At the same time, the dead ball wins: it is the safe reading.
                key = (e["ts"], e["typeId"] in DEAD_BALL)
                if newest is None or key > (newest["ts"], newest["typeId"] in DEAD_BALL):
                    newest = e
        if newest is None or (self._live_ts is not None and newest["ts"] < self._live_ts):
            return None
        if newest["typeId"] in DEAD_BALL:
            message = None if self.last is None and self._live is None else self.clear("dead")
            self._live_ts = newest["ts"]  # only an event after the dead ball (the restart) prices again
            return message
        self._live_ts = newest["ts"]
        team, other = newest["team"], "away" if newest["team"] == "home" else "home"
        if newest["typeId"] in PASSES and newest.get("endX") is not None:
            self._live = (newest["endX"], newest["endY"], team if newest.get("outcome") == 1 else other)
        else:
            self._live = (newest["x"], newest["y"], team)
        return self.tick()

    def tick(self) -> dict | None:
        """Live: re-price for the age of the newest on-ball event; suspend when it is too old. The message, or
        None when nothing changed."""
        if self._live is None:
            return None
        age = self._now() - self._live_ts
        if age > LIVE_MAX_AGE:
            return self.clear("stale")
        x, y, team = self._live
        return self.update(x, y, team, age=max(age, 0.0) + LIVE_AGE_MARGIN, source_ts=self._live_ts)

"""Live odds that move with the play: a display price for each zone, from where the ball is now.

Display only. The roulette still settles from the pot (services/p2proulette.py); these odds tell a viewer how
likely each zone is, given the ball, and never change a payout.

Source: the demo pricing table from tools/build_odds.py (data/pricing/demo_<slug>.json, version 2). Its
conditional rows are in the possessor frame: the team on the ball attacks right. The ball track and the board
are in the home-attacks-right frame, so for an away possessor the ball is mirrored to look up the row, and the
row's cells are mirrored back. The table stores winnings-to-1 (tools/build_pricing.py odds_from); the message
sends decimal odds, the same scale as the pot's "pays now" price.
"""
import json
from pathlib import Path

from services.roulette import CELLS, cells_at, mirror


class LiveOdds:
    def __init__(self, path: str | Path):
        table = json.loads(Path(path).read_text())
        if table.get("version") != 2:
            raise ValueError(f"{path}: expected a version 2 pricing table")
        markets = table["markets"]
        self._by_ball_cell = {int(k): v for k, v in markets["next_pass"]["conditional"]["byBallCell"].items()}
        self._shot = markets["next_shot"]["conditional"]["any"]
        self.last: dict | None = None

    def clear(self) -> dict:
        """After a demo jump the old ball is gone: drop the odds until the next sample."""
        self.last = None
        return {"type": "odds", "markets": None}

    def update(self, x: float, y: float, possession: str | None) -> dict | None:
        """The odds message for this ball sample if the odds changed, else None. With no known possessor the
        last odds stand: the table has no row for a loose ball."""
        if possession not in ("home", "away"):
            return None
        away = possession == "away"
        bx, by = (100 - x, 100 - y) if away else (x, y)
        ball_cell = min(cells_at(bx, by))  # the first cell, as tools/build_odds.py keys its rows

        def board(row: list[float]) -> list[float]:
            """Row of to-1 odds by possessor-frame cell -> decimal odds by board cell (cell n at index n - 1)."""
            out = [0.0] * CELLS
            for i, to_one in enumerate(row):
                out[(mirror(i + 1) if away else i + 1) - 1] = round(to_one + 1, 1)
            return out

        message = {"type": "odds", "possession": possession,
                   "markets": {"next_pass": board(self._by_ball_cell[ball_cell]), "next_shot": board(self._shot)}}
        if message == self.last:
            return None
        self.last = message
        return message

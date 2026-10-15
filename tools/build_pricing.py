"""Build per-cell roulette odds from the Wyscout public dataset (CC BY 4.0).

Usage:
    python -m tools.build_pricing --wyscout-dir PATH [--out data/pricing/wyscout_12x3.json] [--analyze]

Method: a round closes at a random in-play moment and settles on the next qualifying event. So each
event is weighted by min(gap since the previous qualifying event in the same period, void cap):
that is the stretch of close times that would settle on it. Close times with no event within the
cap void and are excluded. Coordinates go to the app's absolute frame (home attacks right,
y = 100 is the top touchline) and through services.roulette.cells_at, so the grid is identical.
"""
import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path

from services.roulette import CELLS, cells_at

COMPETITIONS = ["England", "France", "Germany", "Italy", "Spain", "European_Championship", "World_Cup"]
PASS_RESTARTS = {"Goal kick", "Throw in", "Corner", "Free Kick", "Free kick cross"}
SHOT_RESTARTS = {"Free kick shot", "Penalty"}
ON_BALL_FOULS = {"Foul", "Hand foul", "Late card foul", "Violent Foul"}
# Wyscout stores every goal kick at a placeholder corner, (0,0) or (100,100). The Laws put a goal
# kick inside the goal area; all 17 Opta goal kicks in the 2026-10-12 live capture sit at x <= 5.5,
# 39 <= y <= 62 in the team frame, so the goal-area centre is used.
GOAL_KICK_TEAM_FRAME = (5.0, 50.0)

MARKETS = {
    "next_pass": {
        "label": "Next pass",
        "definition": "Where the first pass after betting closes is played from (open play and restarts).",
        "voidAfterSec": 120,
        "wyscout": lambda e: e["eventName"] == "Pass" or (e["eventName"] == "Free Kick" and e["subEventName"] in PASS_RESTARTS),
    },
    "next_shot": {
        "label": "Next shot",
        "definition": "Where the first shot after betting closes is taken from (including free-kick shots and penalties).",
        "voidAfterSec": 900,
        "wyscout": lambda e: e["eventName"] == "Shot" or (e["eventName"] == "Free Kick" and e["subEventName"] in SHOT_RESTARTS),
    },
    "next_foul": {
        "label": "Next foul",
        "definition": "Where the first foul after betting closes happens.",
        "voidAfterSec": 900,
        "wyscout": lambda e: e["eventName"] == "Foul" and e["subEventName"] in ON_BALL_FOULS,
    },
}


def absolute(e: dict, side: str) -> tuple[float, float]:
    x, y = e["positions"][0]["x"], e["positions"][0]["y"]
    if e["subEventName"] == "Goal kick":
        x, y = GOAL_KICK_TEAM_FRAME
    # Wyscout: team attacks left to right, y runs top to bottom, top = attacker's left (kloppy
    # WyscoutCoordinateSystem). Opta-style y grows toward the attacker's left, so y_opta = 100 - y.
    ax, ay = x, 100.0 - y
    if side == "away":
        ax, ay = 100.0 - ax, 100.0 - ay
    return ax, ay


def load(wyscout_dir: Path):
    sides, periods = {}, defaultdict(list)  # (match, period) -> events sorted later
    for comp in COMPETITIONS:
        for m in json.loads((wyscout_dir / "matches" / f"matches_{comp}.json").read_text()):
            sides[m["wyId"]] = {int(t): d["side"] for t, d in m["teamsData"].items()}
        for e in json.loads((wyscout_dir / "events" / f"events_{comp}.json").read_text()):
            if e["matchPeriod"] in ("1H", "2H"):
                periods[(e["matchId"], e["matchPeriod"])].append(e)
    for evs in periods.values():
        evs.sort(key=lambda e: e["eventSec"])
    return sides, periods


def weights(market: dict, sides, periods, cap: float):
    """Per match: {cell: weight}; plus covered and total in-play seconds."""
    per_match: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    covered = total = 0.0
    events = 0
    for (match, _), evs in periods.items():
        end = evs[-1]["eventSec"]
        total += end
        prev = 0.0
        for e in evs:
            if not market["wyscout"](e):
                continue
            # The event still ends the previous window even when it cannot be placed on the grid.
            w = min(e["eventSec"] - prev, cap)
            prev = e["eventSec"]
            side = sides[match].get(e["teamId"])
            if not e["positions"] or side is None:
                continue
            if w <= 0:
                continue
            cells = cells_at(*absolute(e, side))
            for c in cells:
                per_match[match][c] += w / len(cells)
            covered += w
            events += 1
    return per_match, covered, total, events


def probabilities(per_match) -> list[float]:
    tot = [0.0] * (CELLS + 1)
    for cells in per_match.values():
        for c, w in cells.items():
            tot[c] += w
    s = sum(tot)
    return [t / s for t in tot[1:]]


def bootstrap_ci(per_match, reps: int, seed: int = 7) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    matches = list(per_match.values())
    draws = [[] for _ in range(CELLS)]
    for _ in range(reps):
        tot = [0.0] * (CELLS + 1)
        for m in rng.choices(matches, k=len(matches)):
            for c, w in m.items():
                tot[c] += w
        s = sum(tot)
        for i in range(CELLS):
            draws[i].append(tot[i + 1] / s)
    out = []
    for d in draws:
        d.sort()
        out.append((d[int(0.025 * reps)], d[int(0.975 * reps) - 1]))
    return out


def odds_from(p: float, margin: float, max_odds: float) -> float:
    """Winnings-to-1 after the margin, rounded down to 0.1 so rounding never favours the player."""
    if p <= 0:
        return max_odds
    decimal = 1.0 / (p * (1.0 + margin))
    return max(0.1, min(max_odds, math.floor((decimal - 1.0) * 10) / 10))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wyscout-dir", required=True, type=Path)
    ap.add_argument("--out", default="data/pricing/wyscout_12x3.json", type=Path)
    ap.add_argument("--margin", default=0.06, type=float)
    ap.add_argument("--max-odds", default=500.0, type=float)
    ap.add_argument("--reps", default=200, type=int)
    ap.add_argument("--analyze", action="store_true", help="print void share by cap and exit")
    args = ap.parse_args()

    sides, periods = load(args.wyscout_dir)
    if args.analyze:
        for name, market in MARKETS.items():
            for cap in (60, 120, 300, 600, 900, 1200, 1800):
                _, covered, total, events = weights(market, sides, periods, cap)
                print(f"{name:10s} cap {cap:5d}s  void share {1 - covered / total:6.1%}  events {events}")
        return

    out = {
        "source": {
            "name": "Wyscout public dataset (Pappalardo et al., Scientific Data 6, 236, 2019)",
            "doi": "10.1038/s41597-019-0247-7",
            "url": "https://doi.org/10.6084/m9.figshare.c.4415000.v5",
            "licence": "CC BY 4.0",
            "licenceUrl": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "Contains data from the Wyscout public dataset by L. Pappalardo et al., licensed CC BY 4.0. Odds derived by SpatialBETTING.",
            "competitions": "Premier League, Ligue 1, Bundesliga, Serie A, LaLiga 2017/18; Euro 2016; World Cup 2018",
            "matches": len(sides),
            "caveat": "Wyscout and Opta define and locate events differently (about 13% of probability mass differs against DFL data). Demo prices only; real prices need licensed Opta history.",
        },
        "grid": "12 columns x 3 rows on absolute coordinates; home attacks right; cell = column * 3 + row + 1, row 0 at the top touchline.",
        "method": __doc__.split("Method: ", 1)[1].strip(),
        "margin": args.margin,
        "maxOdds": args.max_odds,
        "markets": {},
    }
    for name, market in MARKETS.items():
        per_match, covered, total, events = weights(market, sides, periods, market["voidAfterSec"])
        p = probabilities(per_match)
        ci = bootstrap_ci(per_match, args.reps)
        odds = [odds_from(pi, args.margin, args.max_odds) for pi in p]
        out["markets"][name] = {
            "label": market["label"],
            "definition": market["definition"],
            "voidAfterSec": market["voidAfterSec"],
            "voidShare": round(1 - covered / total, 4),
            "events": events,
            "matches": len(per_match),
            "returnToPlayer": {"min": round(min(pi * (o + 1) for pi, o in zip(p, odds)), 4),
                               "max": round(max(pi * (o + 1) for pi, o in zip(p, odds)), 4)},
            "cells": [
                {"cell": i + 1, "p": round(p[i], 6), "ci95": [round(ci[i][0], 6), round(ci[i][1], 6)],
                 "fairOdds": round(1 / p[i] - 1, 2) if p[i] > 0 else None, "odds": odds[i]}
                for i in range(CELLS)
            ],
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1))
    for name, m in out["markets"].items():
        ps = [c["p"] for c in m["cells"]]
        print(f"{name}: events {m['events']}, void {m['voidShare']:.1%}, p range {min(ps):.4%}-{max(ps):.2%}, per-cell RTP {m['returnToPlayer']}")


if __name__ == "__main__":
    main()

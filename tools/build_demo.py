"""Build the demo match and its demo-only odds from one DFL match of the Elite Soccer Dataset.

Usage:
    python -m tools.build_demo --dfl-dir PATH [--match DFL-MAT-J03WMX] [--slug j03wmx]

Source: Bassek et al., "An integrated dataset of spatiotemporal and event data in elite soccer",
Scientific Data (2025), CC BY 4.0. Writes:
  data/demo/<slug>.json            Opta-shaped raw events with a demo timeline, played by services/demo.py
  (odds: tools/build_odds.py, trained on the other DFL matches)

Mapping (verified against the files, 2026-10-14):
- Coordinates are metres, 0-105 x 0-68, right-handed: a team attacking +x has y = 68 on its left, which
  is Opta's y = 100. Each half's KickOff names TeamLeft and TeamRight; TeamRight starts on the right and
  attacks toward x = 0 (checked against shot positions and keeper tracking).
- Events are converted to each acting team's own attack-right frame, as Opta sends them; MatchState
  rotates the away team back, so the home team always attacks right on screen.
- Fouls, cards and substitutions have no position. Delete events name no target, so they are dropped.
- BlockedShot maps to Attempt saved (15), as Opta records blocked shots; treat that as uncertain.
- Passes have a recipient but no end location, so no end coordinates are sent.
"""
import argparse
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from services.roulette import CELLS, cells_at

SHOT_TYPES = {"ShotWide": 13, "ShotWoodWork": 14, "SavedShot": 15, "BlockedShot": 15, "SuccessfulShot": 16}
HALF_BREAK = 60  # seconds of half time on the demo timeline (the real break was about 18 minutes)
PRE_MATCH = 5  # seconds of pre-match before the demo kick-off


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def build(dfl_dir: Path, match: str, slug: str, margin: float, max_odds: float, root: Path) -> None:
    info = ET.parse(next(dfl_dir.glob(f"DFL_02_01_matchinformation_*_{match}.xml"))).getroot()
    events_root = ET.parse(next(dfl_dir.glob(f"DFL_03_02_events_raw_*_{match}.xml"))).getroot()
    general = info.find(".//General")
    home_id, away_id = general.get("HomeTeamId"), general.get("GuestTeamId")
    side_of = {home_id: "home", away_id: "away"}
    teams, names = {}, {}
    for team in info.iter("Team"):
        players = list(team.iter("Player"))
        for p in players:
            names[p.get("PersonId")] = p.get("Shortname")
        teams[side_of[team.get("TeamId")]] = {
            "id": team.get("TeamId"), "name": team.get("TeamName"), "kit": team.get("PlayerShirtMainColor"),
            "starters": [p.get("Shortname") for p in players if p.get("Starting") == "true"],
            "subs": [p.get("Shortname") for p in players if p.get("Starting") != "true"],
        }

    raw = sorted(events_root.iter("Event"), key=lambda e: parse_time(e.get("EventTime")))
    starts, ends, attacks_left = {}, {}, {}
    for e in raw:
        k = e[0]
        if k.tag == "KickOff" and k.get("GameSection") in ("firstHalf", "secondHalf"):
            period = 1 if k.get("GameSection") == "firstHalf" else 2
            starts[period] = parse_time(e.get("EventTime"))
            # TeamRight is on the right-hand side, so it attacks toward x = 0.
            attacks_left[period] = k.get("TeamRight")
        if k.tag == "FinalWhistle" and k.get("GameSection") in ("firstHalf", "secondHalf"):
            ends[1 if k.get("GameSection") == "firstHalf" else 2] = parse_time(e.get("EventTime"))
    first_half = (ends[1] - starts[1]).total_seconds()

    def period_of(t: datetime) -> int | None:
        return next((p for p in (1, 2) if starts[p] <= t <= ends[p]), None)

    def timeline(t: datetime, period: int) -> float:
        elapsed = (t - starts[period]).total_seconds()
        return PRE_MATCH + elapsed + (0 if period == 1 else first_half + HALF_BREAK)

    def clock(t: datetime, period: int) -> tuple[int, int]:
        elapsed = (t - starts[period]).total_seconds()
        return int(elapsed // 60) + 45 * (period - 1), int(elapsed % 60)

    def team_frame(e: ET.Element, team_id: str, period: int) -> tuple[float | None, float | None]:
        if e.get("X-Position") is None:
            return None, None
        x, y = float(e.get("X-Position")) / 105 * 100, float(e.get("Y-Position")) / 68 * 100
        if attacks_left[period] == team_id:
            x, y = 100 - x, 100 - y
        return round(x, 1), round(y, 1)

    out, next_id = [], 1

    def emit(t: float, period: int, minute: int, second: int, type_id: int, team_id: str | None, **extra) -> dict:
        nonlocal next_id
        event = {"id": next_id, "seqId": next_id, "typeId": type_id, "periodId": period, "timeMin": minute,
                 "timeSec": second, "contestantId": team_id or "", "outcome": extra.pop("outcome", 1),
                 "x": extra.pop("x", 0), "y": extra.pop("y", 0), "qualifier": extra.pop("qualifier", []), **extra}
        next_id += 1
        out.append({"t": round(t, 2), "event": event})
        return event

    # Pre-match: line-ups (Opta "team set up" with qualifier 30 names and 131 slots, 0 = bench).
    for side in ("home", "away"):
        team = teams[side]
        slots = [str(i + 1) for i in range(len(team["starters"]))] + ["0"] * len(team["subs"])
        emit(0, 16, 0, 0, 34, team["id"], primaryKitColour=team["kit"], qualifier=[
            {"qualifierId": 30, "value": "", "detail": ", ".join(team["starters"] + team["subs"])},
            {"qualifierId": 131, "value": ", ".join(slots)}])

    for e in raw:
        t = parse_time(e.get("EventTime"))
        period = period_of(t)
        k = e[0]
        if period is None:
            continue
        minute, second = clock(t, period)
        at = timeline(t, period)
        elapsed = (t - starts[period]).total_seconds()
        if k.tag == "KickOff" and k.get("GameSection") in ("firstHalf", "secondHalf"):
            emit(at - 0.01, period, minute, second, 32, home_id)
            emit(at - 0.01, period, minute, second, 32, away_id)
        if k.tag == "FinalWhistle" and k.get("GameSection") in ("firstHalf", "secondHalf"):
            for tid in (home_id, away_id):
                emit(at, period, minute, second, 30, tid, qualifier=[{"qualifierId": 209}] if period == 2 else [])
            continue
        if k.tag == "CornerKick":
            # The corner itself; its delivery is the Play inside it and is sent as a pass below.
            emit(at, period, minute, second, 6, k.get("Team"))
        play = e.find(".//Play")
        if play is not None:
            team_id = play.get("Team")
            x, y = team_frame(e, team_id, period)
            outcome = 1 if play.get("Evaluation") in ("successfullyCompleted", "successful") else 0
            emit(at, period, minute, second, 1, team_id, outcome=outcome, playerName=names.get(play.get("Player")),
                 x=x if x is not None else 0, y=y if y is not None else 0)
            continue
        shot = e.find(".//ShotAtGoal")
        if shot is not None:
            result = shot[0].tag if len(shot) else None
            type_id = SHOT_TYPES.get(result)
            if type_id is None:
                continue
            team_id = shot.get("Team")
            x, y = team_frame(e, team_id, period)
            qualifier = [{"qualifierId": 9}] if k.tag == "Penalty" else []
            if k.tag == "Penalty" and k.get("DecisionTimestamp"):
                # The award: Opta sends a foul-won event with the penalty qualifier. DFL records only
                # the decision time, so this event has no position.
                award = parse_time(k.get("DecisionTimestamp"))
                if period_of(award) == period:
                    am, asec = clock(award, period)
                    emit(timeline(award, period), period, am, asec, 4, team_id, outcome=1, qualifier=[{"qualifierId": 9}],
                         playerName=names.get(k.get("ProspectiveTaker")))
            emit(at, period, minute, second, type_id, team_id, playerName=names.get(shot.get("Player")),
                 x=x if x is not None else 0, y=y if y is not None else 0, qualifier=qualifier)
            continue
        if k.tag == "Foul":
            emit(at, period, minute, second, 4, k.get("TeamFouler"), outcome=0, playerName=names.get(k.get("Fouler")))
            emit(at, period, minute, second, 4, k.get("TeamFouled"), outcome=1, playerName=names.get(k.get("Fouled")))
        elif k.tag == "Caution":
            q = {"yellow": 31, "yellowRed": 32, "red": 33}.get(k.get("CardColor"))
            if q:
                emit(at, period, minute, second, 17, k.get("Team"), playerName=names.get(k.get("Player")), qualifier=[{"qualifierId": q}])
        elif k.tag == "Substitution":
            emit(at, period, minute, second, 18, k.get("Team"), playerName=names.get(k.get("PlayerOut")))
            emit(at, period, minute, second, 19, k.get("Team"), playerName=names.get(k.get("PlayerIn")))

    out.sort(key=lambda item: (item["t"], item["event"]["id"]))
    demo = {
        "slug": slug, "source": "Bassek et al., Scientific Data (2025), Elite Soccer Dataset, CC BY 4.0",
        "dflMatchId": match, "competition": general.get("CompetitionName"), "season": general.get("Season"),
        "matchDay": general.get("MatchDay"), "result": general.get("Result"),
        # Real ends, for display: the periods in which the home team attacks toward x = 0 (the left).
        "homeAttacksLeft": [p for p in (1, 2) if attacks_left[p] == home_id],
        "fixture": {"fixtureId": f"demo-{slug}", "startTime": starts[1].isoformat().replace("+00:00", "Z"),
                    "teams": [{"id": teams["home"]["id"], "name": teams["home"]["name"], "association": "HOME"},
                              {"id": teams["away"]["id"], "name": teams["away"]["name"], "association": "AWAY"}]},
        "events": out,
    }
    (root / "data" / "demo").mkdir(parents=True, exist_ok=True)
    (root / "data" / "demo" / f"{slug}.json").write_text(json.dumps(demo, ensure_ascii=False))

    # Demo odds are built by tools/build_odds.py, from the other DFL matches (out of sample).
    print(f"{len(out)} events, P1 {first_half:.0f}s, demo length {out[-1]['t']:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dfl-dir", required=True, type=Path)
    ap.add_argument("--match", default="DFL-MAT-J03WMX")
    ap.add_argument("--slug", default="j03wmx")
    ap.add_argument("--margin", default=0.06, type=float)
    ap.add_argument("--max-odds", default=500.0, type=float)
    args = ap.parse_args()
    build(args.dfl_dir, args.match, args.slug, args.margin, args.max_odds, Path(__file__).resolve().parent.parent)


if __name__ == "__main__":
    main()

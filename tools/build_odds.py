"""Build out-of-sample demo odds for one DFL match from the other DFL matches of the Elite Soccer Dataset.

Usage:
    python -m tools.build_odds --dfl-dir PATH [--match DFL-MAT-J03WMX] [--slug j03wmx]

Source: Bassek et al., Scientific Data (2025), Elite Soccer Dataset, CC BY 4.0. The demo match is never
used for training; it is used only for the out-of-sample checks printed at the end. Writes:
  data/pricing/demo_<slug>.json       next pass / next shot, conditional on the ball (version 2)
  data/pricing/demo_www_<slug>.json   Who Where When tables by playing position (version 2)

Frames:
- DFL event positions are absolute metres; tools/build_demo.py team_frame() turns them into the acting
  team's attack-right frame. The ball track (tools/build_ball.extract) is in the home-attacks-right frame.
- The "possessor" frame: the team on the ball attacks right. For an away possessor the ball is mirrored
  (x -> 100 - x, y -> 100 - y); an event by the other team is mirrored from its own frame.
- Line ties: every touching cell wins in full (services/roulette.py cells_at), so a row can sum above 1.

Next pass: every 5 Hz ball sample inside a half with a known possessor settles on the first Play with a
position strictly after it, within 120 s, in the same half; otherwise it is void. Each sample weighs
0.2 s. W[in][out], in = first cell of the ball, each row shrunk toward the marginal M with 60 s:
P[in][out] = (W[in][out] + 60 M[out]) / (row[in] + 60).
Next pass by age (live): the price is read from older information. For a lag L in LAGS, each sample at t settles
on the first Play after t as above, but the ball cell and the possessor come from the sample at t - L; shrunk the
same way. Lag 0 is the table above.
Next shot: the same samples settle on the first ShotAtGoal with a position after them in the half (no cap).
Who Where When: xG per 90 by DFL PlayingPosition code, and the shot-xG zone table per code (shooter's
attack-right frame) shrunk toward the overall table with 1.0 xG.
"""
import argparse
import bisect
import json
import random
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from services.roulette import CELLS, cells_at
from tools.build_ball import extract
from tools.build_demo import parse_time
from tools.build_pricing import odds_from

STEP = 0.2  # seconds per 5 Hz sample
PASS_VOID = 120.0
PASS_ALPHA = 60.0  # seconds
# Information ages for the live next-pass tables, seconds. A live Opta event reaches us a few seconds after the play.
LAGS = (0, 2, 4, 6, 8, 10, 15)
ZONE_ALPHA = 1.0  # xG
MIN_MINUTES = 90.0
MAX_MEDIAN_M = 6.0
MIN_AGREE = 0.8
MARGIN, MAX_ODDS, WWW_MAX_ODDS = 0.06, 500.0, 1000.0
SOURCE = "Bassek et al., Scientific Data (2025), Elite Soccer Dataset, CC BY 4.0"
SECTIONS = {"firstHalf": 1, "secondHalf": 2}


def mirror_xy(x: float, y: float) -> tuple[float, float]:
    return round(100 - x, 1), round(100 - y, 1)


def load_events(dfl_dir: Path, match: str, starts: dict, ends: dict, attacks_left: dict) -> dict:
    """Passes, shots, substitutions and red cards of one match, per period, in elapsed seconds."""
    root = ET.parse(next(dfl_dir.glob(f"DFL_03_02_events_raw_*_{match}.xml"))).getroot()
    out = {"passes": {1: [], 2: []}, "shots": {1: [], 2: []}, "subs": [], "reds": []}

    def team_frame(e: ET.Element, team_id: str, period: int) -> tuple[float, float]:
        x, y = float(e.get("X-Position")) / 105 * 100, float(e.get("Y-Position")) / 68 * 100
        if attacks_left[period] == team_id:
            x, y = 100 - x, 100 - y
        return round(x, 1), round(y, 1)

    for e in root.iter("Event"):
        t = parse_time(e.get("EventTime"))
        period = next((p for p in (1, 2) if starts[p] <= t <= ends[p]), None)
        k = e[0]
        if k.tag == "Substitution":
            # A change at half time (or before kick-off) takes effect at the start of the next half.
            when = (period, (t - starts[period]).total_seconds()) if period else ((2, 0.0) if t > ends[1] else (1, 0.0))
            out["subs"].append((*when, k.get("Team"), k.get("PlayerOut"), k.get("PlayerIn"), k.get("PlayingPosition")))
        if period is None:
            continue
        el = (t - starts[period]).total_seconds()
        if k.tag == "Caution" and k.get("CardColor") in ("red", "yellowRed"):
            out["reds"].append((period, el, k.get("Player")))
        play = e.find(".//Play")
        if play is not None and e.get("X-Position") is not None:
            team = play.get("Team")
            out["passes"][period].append((el, team, *team_frame(e, team, period)))
            continue  # same rule as tools/build_demo.py: an event with a Play is a pass
        shot = e.find(".//ShotAtGoal")
        if shot is not None and e.get("X-Position") is not None:
            team = shot.get("Team")
            out["shots"][period].append((el, team, *team_frame(e, team, period), float(shot.get("xG") or 0.0), shot.get("Player")))
    for p in (1, 2):
        out["passes"][p].sort()
        out["shots"][p].sort()
    return out


def outcomes(track: dict, ev: dict):
    """Yield (period, live, in_cell, next pass cells or None, pass index or None, shot cells or None, shot index or None).

    Everything is in the possessor frame. Only samples with a known possessor are yielded.
    """
    home = track["homeId"]
    for period in (1, 2):
        passes, shots = ev["passes"][period], ev["shots"][period]
        p_times, s_times = [p[0] for p in passes], [s[0] for s in shots]
        for t, x, y, live, poss in track["samples"][period]:
            if poss not in (1, 2):
                continue
            possessor_home = poss == 1
            bx, by = (x, y) if possessor_home else mirror_xy(x, y)
            in_cell = min(cells_at(bx, by))

            def in_frame(team: str, ex: float, ey: float) -> set[int]:
                return cells_at(ex, ey) if (team == home) == possessor_home else cells_at(*mirror_xy(ex, ey))

            pc = pi = sc = si = None
            i = bisect.bisect_right(p_times, t)
            if i < len(passes) and passes[i][0] - t <= PASS_VOID:
                pc, pi = in_frame(passes[i][1], passes[i][2], passes[i][3]), (period, i)
            j = bisect.bisect_right(s_times, t)
            if j < len(shots):
                sc, si = in_frame(shots[j][1], shots[j][2], shots[j][3]), (period, j)
            yield period, live, in_cell, pc, pi, sc, si


def lagged(track: dict, ev: dict):
    """Yield (lag, in_cell, next pass cells): the ball cell and possessor from the sample `lag` seconds before the
    sample whose next pass settles it, all in the possessor frame of that earlier sample."""
    home = track["homeId"]
    for period in (1, 2):
        passes, samples = ev["passes"][period], track["samples"][period]
        p_times = [p[0] for p in passes]
        for i, (t, *_rest) in enumerate(samples):
            k = bisect.bisect_right(p_times, t)
            if k >= len(passes) or passes[k][0] - t > PASS_VOID:
                continue
            team, ex, ey = passes[k][1], passes[k][2], passes[k][3]
            for lag in LAGS:
                j = i - round(lag / STEP)
                if j < 0:
                    break
                _, x, y, _live, poss = samples[j]
                if poss not in (1, 2):
                    continue
                possessor_home = poss == 1
                bx, by = (x, y) if possessor_home else mirror_xy(x, y)
                out = cells_at(ex, ey) if (team == home) == possessor_home else cells_at(*mirror_xy(ex, ey))
                yield lag, min(cells_at(bx, by)), out


def frame_checks(track: dict, ev: dict) -> tuple[float, float, float]:
    """Pass positions vs the ball, in the passer's frame; and shots in the attacking half.

    Returns (median metres from a pass to the ball at the pass time, share of passes by the possessor
    team, share of shots with x > 50 in the shooter's frame).
    """
    home = track["homeId"]
    dists, own, total = [], 0, 0
    for period in (1, 2):
        samples = track["samples"][period]
        ts = [s[0] for s in samples]
        for el, team, x, y in ev["passes"][period]:
            i = min(max(bisect.bisect_left(ts, el), 0), len(ts) - 1)
            s = samples[i]
            bx, by = (s[1], s[2]) if team == home else mirror_xy(s[1], s[2])
            dists.append((((bx - x) * 1.05) ** 2 + ((by - y) * 0.68) ** 2) ** 0.5)
            if s[4] in (1, 2):
                total += 1
                own += (s[4] == 1) == (team == home)
    shots = [s for p in (1, 2) for s in ev["shots"][p]]
    attacking = sum(1 for s in shots if s[2] > 50) / len(shots)
    dists.sort()
    return dists[len(dists) // 2], own / total, attacking


def players_and_minutes(dfl_dir: Path, match: str, track: dict, ev: dict):
    """Per player: on-pitch intervals and position code; returns (code by player at a time, minutes per code, names)."""
    info = ET.parse(next(dfl_dir.glob(f"DFL_02_01_matchinformation_*_{match}.xml"))).getroot()
    side_of = {track["homeId"]: "home", track["awayId"]: "away"}
    names, team_of, code = {}, {}, {}
    on = {}  # player -> (period, elapsed) when he came on
    spells = []  # (player, code, (period, from), (period, to))
    for team in info.iter("Team"):
        for p in team.iter("Player"):
            pid = p.get("PersonId")
            names[pid], team_of[pid] = p.get("Shortname"), side_of[team.get("TeamId")]
            if p.get("Starting") == "true":
                code[pid] = p.get("PlayingPosition")
                on[pid] = (1, 0.0)
    came_on = {}
    off_events = sorted([(p, el, out, "sub", pin, pos) for p, el, _, out, pin, pos in ev["subs"]]
                        + [(p, el, pid, "red", None, None) for p, el, pid in ev["reds"]])
    for period, el, out, kind, pin, pos in off_events:
        if out in on:
            spells.append((out, code[out], on.pop(out), (period, el)))
        if kind == "sub":
            code[pin] = pos
            on[pin] = (period, el)
            came_on[pin] = pos
    end = (2, (track["ends"][2] - track["starts"][2]).total_seconds())
    for pid, start in on.items():
        spells.append((pid, code[pid], start, end))
    half_len = {p: (track["ends"][p] - track["starts"][p]).total_seconds() for p in (1, 2)}

    def seconds(a: tuple, b: tuple) -> float:
        if a[0] == b[0]:
            return b[1] - a[1]
        return (half_len[a[0]] - a[1]) + b[1]

    minutes = defaultdict(float)
    for pid, c, a, b in spells:
        minutes[c] += seconds(a, b) / 60

    def code_at(pid: str, period: int, el: float) -> str | None:
        for p, c, a, b in spells:
            if p == pid and a <= (period, el) <= b:
                return c
        return None

    return code_at, minutes, names, team_of, info, came_on


def build(dfl_dir: Path, match: str, slug: str, root: Path) -> None:
    all_matches = sorted(f.stem.split("_")[-1] for f in dfl_dir.glob("DFL_03_02_events_raw_*.xml"))
    train = [m for m in all_matches if m != match]
    print(f"demo {match}; candidate training matches {train}")

    W = [[0.0] * (CELLS + 1) for _ in range(CELLS + 1)]
    row_secs = [0.0] * (CELLS + 1)  # sample seconds per ball cell (a line tie counts its sample once)
    S = [0.0] * (CELLS + 1)
    WL = {lag: [[0.0] * (CELLS + 1) for _ in range(CELLS + 1)] for lag in LAGS}
    rows_L = {lag: [0.0] * (CELLS + 1) for lag in LAGS}
    pass_seconds = shot_seconds = 0.0
    passes_used, shots_used = set(), set()
    zone_code = defaultdict(lambda: [0.0] * (CELLS + 1))
    xg_code, min_code = defaultdict(float), defaultdict(float)
    stoppage = {1: [], 2: []}
    trained = []
    for m in train:
        try:
            track = extract(dfl_dir, m, MAX_MEDIAN_M, MIN_AGREE)
        except SystemExit as exc:
            print(f"SKIP {m}: {exc}")
            continue
        trained.append(m)
        ev = load_events(dfl_dir, m, track["starts"], track["ends"], track["attacksLeft"])
        med, own, att = frame_checks(track, ev)
        print(f"CHECK {m}: transform median {track['medianM']:.2f} m at {track['offset']:+.2f} s; possession agree "
              f"{track['agreement']:.1%} of {track['checked']}; pass-to-ball median {med:.2f} m; passes by possessor "
              f"{own:.1%}; shots x>50 in shooter frame {att:.1%}")
        for period, live, in_cell, pc, pi, sc, si in outcomes(track, ev):
            if pc is not None:
                for c in pc:
                    W[in_cell][c] += STEP
                row_secs[in_cell] += STEP
                pass_seconds += STEP
                passes_used.add((m, pi))
            if sc is not None:
                for c in sc:
                    S[c] += STEP
                shot_seconds += STEP
                shots_used.add((m, si))
        for lag, in_cell, out in lagged(track, ev):
            for c in out:
                WL[lag][in_cell][c] += STEP
            rows_L[lag][in_cell] += STEP
        for p in (1, 2):
            stoppage[p].append(max(0.0, (track["ends"][p] - track["starts"][p]).total_seconds() - 45 * 60))
        code_at, minutes, *_ = players_and_minutes(dfl_dir, m, track, ev)
        for c, v in minutes.items():
            min_code[c] += v
        for period in (1, 2):
            for el, team, x, y, xg, player in ev["shots"][period]:
                c = code_at(player, period, el)
                if c is None:
                    print(f"  WARN {m}: shooter {player} not on the pitch at P{period} {el:.0f}s")
                    continue
                xg_code[c] += xg
                for cell in cells_at(x, y):
                    zone_code[c][cell] += xg

    # ---- next pass ----
    col = [sum(W[i][c] for i in range(1, CELLS + 1)) for c in range(CELLS + 1)]
    M = [v / pass_seconds for v in col]
    P = {i: [(W[i][c] + PASS_ALPHA * M[c]) / (row_secs[i] + PASS_ALPHA) for c in range(1, CELLS + 1)]
         for i in range(1, CELLS + 1)}
    S_p = [v / shot_seconds for v in S[1:]]
    odds_P = {i: [odds_from(p, MARGIN, MAX_ODDS) for p in P[i]] for i in P}
    odds_S = [odds_from(p, MARGIN, MAX_ODDS) for p in S_p]
    odds_L = {lag: {i: [odds_from((WL[lag][i][c] + PASS_ALPHA * M[c]) / (rows_L[lag][i] + PASS_ALPHA), MARGIN, MAX_ODDS)
                        for c in range(1, CELLS + 1)] for i in range(1, CELLS + 1)} for lag in LAGS}

    def table(ps: list[float]) -> list[dict]:
        return [{"cell": c + 1, "p": round(ps[c], 6), "odds": odds_from(ps[c], MARGIN, MAX_ODDS)} for c in range(CELLS)]

    pricing = {
        "source": f"{SOURCE}; trained on {len(trained)} DFL matches, the demo match {match} left out",
        "grid": "12 columns x 3 rows; cell = column * 3 + row + 1, row 0 at the top (y = 100). Conditional tables are in "
                "the possessor frame: the team on the ball attacks right; mirror cells (services/roulette.py mirror) for the away team.",
        "method": __doc__.split("Frames:", 1)[1].strip(),
        "margin": MARGIN, "maxOdds": MAX_ODDS, "demoOnly": True, "version": 2, "trainedOn": trained,
        "markets": {
            "next_pass": {
                "label": "Next pass",
                "definition": "Where the first pass after you place the bet is played from (open play and restarts).",
                "voidAfterSec": int(PASS_VOID), "instant": True, "cells": table(M[1:]),
                "conditional": {"frame": "possessor", "byBallCell": {str(i): odds_P[i] for i in range(1, CELLS + 1)},
                                # Live: the price from information this many seconds old (see "Next pass by age").
                                "byLag": {str(lag): {str(i): odds_L[lag][i] for i in range(1, CELLS + 1)}
                                          for lag in LAGS}},
                "samples": round(pass_seconds, 1), "events": len(passes_used),
            },
            "next_shot": {
                "label": "Next shot", "definition": "Where the next shot is taken from (including penalties).",
                "voidAfterSec": None, "instant": True, "cells": table(S_p),
                "conditional": {"frame": "possessor", "any": odds_S},
                "samples": round(shot_seconds, 1), "events": len(shots_used),
            },
        },
    }
    (root / "data" / "pricing" / f"demo_{slug}.json").write_text(json.dumps(pricing, indent=1))

    # ---- Who Where When ----
    non_gk = [c for c in min_code if c != "TW"]
    overall_rate = sum(xg_code[c] for c in non_gk) / sum(min_code[c] for c in non_gk) * 90
    low = {c: round(v, 1) for c, v in sorted(min_code.items()) if v < MIN_MINUTES}
    rate = {c: (round(xg_code[c] / min_code[c] * 90, 4) if min_code[c] >= MIN_MINUTES else round(overall_rate, 4))
            for c in sorted(min_code)}
    zone_total = [sum(z[c] for z in zone_code.values()) for c in range(CELLS + 1)]
    total_xg = sum(xg_code.values())
    zone = [v / total_xg for v in zone_total[1:]]

    demo_track = extract(dfl_dir, match, MAX_MEDIAN_M, MIN_AGREE)
    demo_ev = load_events(dfl_dir, match, demo_track["starts"], demo_track["ends"], demo_track["attacksLeft"])
    _, _, names, team_of, info, came_on = players_and_minutes(dfl_dir, match, demo_track, demo_ev)
    players = {"home": {}, "away": {}}
    for p in info.iter("Player"):
        pid = p.get("PersonId")
        c = p.get("PlayingPosition") if p.get("Starting") == "true" else came_on.get(pid)
        players[team_of[pid]][names[pid]] = c
    codes = sorted(set(min_code) | set(xg_code) | {c for side in players.values() for c in side.values() if c})
    for c in codes:
        rate.setdefault(c, round(overall_rate, 4))
    zone_by = {c: [round((zone_code[c][cell] + ZONE_ALPHA * zone[cell - 1]) / (xg_code.get(c, 0.0) + ZONE_ALPHA), 6)
                   for cell in range(1, CELLS + 1)] for c in codes}
    www = {
        "slug": slug, "version": 2, "source": f"{SOURCE}; trained on {len(trained)} DFL matches, the demo match {match} left out",
        "method": "Given that a goal comes: P(scorer) from xG per 90 by DFL PlayingPosition code; P(zone | code) = shot-xG share "
                  "per cell in the shooter's attack-right frame, shrunk toward the overall table with 1.0 xG (line ties count "
                  "in full); P(band | time left) with the mean stoppage time. Legs treated as independent; demo only.",
        "margin": MARGIN, "maxOdds": WWW_MAX_ODDS, "demoOnly": True, "trainedOn": trained,
        "positionXgPer90": rate,
        "minutesByPosition": {c: round(min_code.get(c, 0.0), 1) for c in codes},
        "lowMinutesCodes": low, "fallbackXgPer90": round(overall_rate, 4),
        "zoneByPosition": zone_by, "zone": [round(z, 6) for z in zone],
        "players": players,
        "stoppageSeconds": {"firstHalf": round(sum(stoppage[1]) / len(stoppage[1]), 1),
                            "secondHalf": round(sum(stoppage[2]) / len(stoppage[2]), 1)},
    }
    (root / "data" / "pricing" / f"demo_www_{slug}.json").write_text(json.dumps(www, indent=1, ensure_ascii=False))

    # ---- sanity checks ----
    print(f"\nnext_pass: {pass_seconds:.0f} s of samples, {len(passes_used)} passes; next_shot: {shot_seconds:.0f} s, {len(shots_used)} shots")

    def block(i: int) -> set[int]:
        col, row = (i - 1) // 3, (i - 1) % 3
        return {c * 3 + r + 1 for c in range(col - 1, col + 2) for r in range(row - 1, row + 2) if 0 <= c < 12 and 0 <= r < 3}

    near = [sum(P[i][c - 1] for c in block(i)) for i in range(1, CELLS + 1)]
    weighted = sum(near[i - 1] * row_secs[i] for i in range(1, CELLS + 1)) / sum(row_secs)
    print(f"next_pass mass in own cell + neighbours: mean over rows {sum(near) / CELLS:.3f}, sample-weighted {weighted:.3f}; "
          f"row sums {min(sum(P[i]) for i in P):.4f}-{max(sum(P[i]) for i in P):.4f}; own cell p mean "
          f"{sum(P[i][i - 1] for i in P) / CELLS:.3f}")
    print(f"next_shot S: sum {sum(S_p):.4f}; top cells " + ", ".join(f"{c + 1}:{S_p[c]:.3f}" for c in sorted(range(CELLS), key=lambda c: -S_p[c])[:5]))

    med, own, att = frame_checks(demo_track, demo_ev)
    print(f"DEMO {match}: transform median {demo_track['medianM']:.2f} m at {demo_track['offset']:+.2f} s; possession agree "
          f"{demo_track['agreement']:.1%}; pass-to-ball median {med:.2f} m; passes by possessor {own:.1%}; shots x>50 {att:.1%}")
    rng = random.Random(7)
    for scope in ("live", "all"):
        n = own_ret = rand_ret = exp_rand = own_p = own_win = rnd_ret = 0.0
        sn = s_rand = s_top = s_rnd = 0.0
        top = max(range(CELLS), key=lambda c: S_p[c]) + 1
        bins = defaultdict(lambda: [0.0, 0.0, 0])
        for period, live, in_cell, pc, pi, sc, si in outcomes(demo_track, demo_ev):
            if scope == "live" and not live:
                continue
            if pc is not None:
                n += 1
                o = odds_P[in_cell]
                own_ret += (o[in_cell - 1] + 1) if in_cell in pc else 0
                own_p += P[in_cell][in_cell - 1]
                own_win += in_cell in pc
                rand_ret += sum((o[c - 1] + 1) for c in pc) / CELLS
                pick = rng.randint(1, CELLS)
                rnd_ret += (o[pick - 1] + 1) if pick in pc else 0
                for c in range(1, CELLS + 1):
                    b = bins[min(int(P[in_cell][c - 1] * 10), 9)]
                    b[0] += P[in_cell][c - 1]
                    b[1] += c in pc
                    b[2] += 1
            if sc is not None:
                sn += 1
                s_rand += sum((odds_S[c - 1] + 1) for c in sc) / CELLS
                s_top += (odds_S[top - 1] + 1) if top in sc else 0
                pick = rng.randint(1, CELLS)
                s_rnd += (odds_S[pick - 1] + 1) if pick in sc else 0
        print(f"DEMO next_pass ({scope}, {int(n)} settled samples): RTP own cell {own_ret / n:.3f} (priced p {own_p / n:.3f}, "
              f"won {own_win / n:.3f}); RTP uniform cell {rand_ret / n:.3f}; one random draw {rnd_ret / n:.3f}")
        print("  calibration (priced p bin: mean p / hit rate / pairs): " + "; ".join(
            f"{k / 10:.1f}: {v[0] / v[2]:.3f}/{v[1] / v[2]:.3f}/{v[2]}" for k, v in sorted(bins.items())))
        print(f"DEMO next_shot ({scope}, {int(sn)} settled samples): RTP uniform cell {s_rand / sn:.3f}; top cell {top} "
              f"{s_top / sn:.3f}; one random draw {s_rnd / sn:.3f}")
    print("\npositionXgPer90 (code: xG/90, minutes, xG):")
    for c in codes:
        print(f"  {c}: {rate[c]:.3f}  {min_code.get(c, 0):.0f} min  {xg_code.get(c, 0):.2f} xG" + ("  (fallback)" if c in low or c not in min_code else ""))
    print(f"  overall non-GK {overall_rate:.3f}; codes under {MIN_MINUTES:.0f} min: {low}")
    for c in ("STZ", "STL", "STR"):
        if c in zone_by:
            z = zone_by[c]
            order = sorted(range(CELLS), key=lambda i: -z[i])
            print(f"zone {c}: top " + ", ".join(f"{i + 1}:{z[i]:.3f}" for i in order[:4]) + "; bottom "
                  + ", ".join(f"{i + 1}:{z[i]:.4f}" for i in order[-3:]) + f"; sum {sum(z):.3f}")
    print(f"players: {players}")
    print(f"stoppage {www['stoppageSeconds']}; trained on {trained}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dfl-dir", required=True, type=Path)
    ap.add_argument("--match", default="DFL-MAT-J03WMX")
    ap.add_argument("--slug", default="j03wmx")
    args = ap.parse_args()
    build(args.dfl_dir, args.match, args.slug, Path(__file__).resolve().parent.parent)


if __name__ == "__main__":
    main()

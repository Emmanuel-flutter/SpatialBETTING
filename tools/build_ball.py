"""Build the ball track for the demo match from the DFL positions file of the Elite Soccer Dataset.

Usage:
    python -m tools.build_ball --dfl-dir PATH [--match DFL-MAT-J03WMX] [--slug j03wmx]

Source: Bassek et al., "An integrated dataset of spatiotemporal and event data in elite soccer",
Scientific Data (2025), CC BY 4.0. Writes:
  data/demo/<slug>_ball.json   [t, x, y, live, possession] samples at 5 Hz on the demo timeline of
                               tools/build_demo.py; possession is 1 = home, 2 = away, 0 = not given
extract(dfl_dir, match) returns the same track per half (seconds since KickOff) for other tools
(tools/build_odds.py), without writing a file.

Method:
- The positions file (about 420 MB) is streamed with iterparse. Only the FrameSets with TeamId="BALL"
  (one per half) are read; every Frame is removed from the tree after it is read.
- The transform from ball (X, Y) to event metres (0-105 x 0-68) is found empirically: for every Play or
  ShotAtGoal event with a position, the ball frame nearest to the event time is compared under each
  candidate transform and time offset, and the smallest median distance wins. The run stops if that
  median is above MAX_MEDIAN_M.
- The time offset is defined as event clock = frame T + offset.
- Event metres go to percent, then to the app frame: the home team always attacks right (x = 100), the
  same per-half flip as team_frame() in tools/build_demo.py for the home team.
- The timeline is the same as tools/build_demo.py: PRE_MATCH + elapsed since that half's KickOff, plus
  the first half and HALF_BREAK in the second half. Only samples between KickOff and FinalWhistle are kept.
"""
import argparse
import bisect
import json
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from tools.build_demo import HALF_BREAK, PRE_MATCH

HZ = 5
FRAME_STEP = 5  # 25 Hz source -> 5 Hz
# Display only: open-play passes have a median of 4.33 m; set-piece event times do not mark the ball moment.
MAX_MEDIAN_M = 5.0
OFFSETS = [round(i * 0.04, 2) for i in range(-50, 51)]  # -2 s .. +2 s
TRANSFORMS = {
    "x=X+52.5, y=Y+34": lambda X, Y: (X + 52.5, Y + 34),
    "x=X+52.5, y=34-Y": lambda X, Y: (X + 52.5, 34 - Y),
}
SECTIONS = {"firstHalf": 1, "secondHalf": 2}


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def read_ball(path: Path) -> dict[int, list[tuple[float, float, float, int, int]]]:
    """Return {period: [(epoch seconds, X, Y, BallStatus, BallPossession), ...]} for the ball, streamed."""
    frames: dict[int, list] = {1: [], 2: []}
    frameset, period = None, None
    for kind, elem in ET.iterparse(path, events=("start", "end")):
        if elem.tag == "FrameSet":
            if kind == "start":
                frameset = elem
                period = SECTIONS.get(elem.get("GameSection")) if elem.get("TeamId") == "BALL" else None
            else:
                elem.clear()
                frameset, period = None, None
        elif elem.tag == "Frame" and kind == "end":
            if period is not None:
                frames[period].append((parse_time(elem.get("T")).timestamp(), float(elem.get("X")),
                                       float(elem.get("Y")), int(elem.get("BallStatus", "0")),
                                       int(elem.get("BallPossession", "0"))))
            if frameset is not None:
                frameset.remove(elem)  # keep memory flat: the FrameSet never holds more than one Frame
    for p in frames:
        frames[p].sort()
    return frames


def nearest(times: list[float], t: float) -> int:
    i = bisect.bisect_left(times, t)
    if i == 0:
        return 0
    if i == len(times):
        return len(times) - 1
    return i if times[i] - t < t - times[i - 1] else i - 1


def extract(dfl_dir: Path, match: str, max_median_m: float = MAX_MEDIAN_M, min_agree: float = 0.8) -> dict:
    """Read one match's ball track. Raises SystemExit when the transform or possession check fails.

    Returns {"samples": {1: [[t_rel, x, y, live, poss], ...], 2: [...]}, "starts", "ends", "attacksLeft",
    "homeId", "awayId", "firstHalf", "transform", "offset", "medianM", "agreement", "checked", ...}.
    t_rel is seconds since that half's KickOff (event clock), 5 Hz; x, y are percent with the home team
    attacking right (rounded to 0.1, as the demo track); live is 1 when BallStatus is 1; poss is 1 = home,
    2 = away, 0 = not given. "starts"/"ends" are UTC datetimes per period; "attacksLeft" the team id per period.
    """
    info = ET.parse(next(dfl_dir.glob(f"DFL_02_01_matchinformation_*_{match}.xml"))).getroot()
    general = info.find(".//General")
    home_id, away_id = general.get("HomeTeamId"), general.get("GuestTeamId")
    events_root = ET.parse(next(dfl_dir.glob(f"DFL_03_02_events_raw_*_{match}.xml"))).getroot()

    starts, ends, attacks_left, anchors, goals, passes = {}, {}, {}, [], [], []
    for e in events_root.iter("Event"):
        k = e[0]
        t = parse_time(e.get("EventTime"))
        if k.tag == "KickOff" and k.get("GameSection") in SECTIONS:
            starts[SECTIONS[k.get("GameSection")]] = t
            attacks_left[SECTIONS[k.get("GameSection")]] = k.get("TeamRight")
        if k.tag == "FinalWhistle" and k.get("GameSection") in SECTIONS:
            ends[SECTIONS[k.get("GameSection")]] = t
        if e.get("X-Position") is not None and (e.find(".//Play") is not None or e.find(".//ShotAtGoal") is not None):
            anchors.append((t.timestamp(), float(e.get("X-Position")), float(e.get("Y-Position"))))
        play = e.find(".//Play")
        if play is not None and play.get("FromOpenPlay") == "true":
            passes.append((t.timestamp(), play.get("Team")))
        shot = e.find(".//ShotAtGoal")
        if shot is not None and len(shot) and shot[0].tag == "SuccessfulShot":
            goals.append((t, shot.get("Team")))
    first_half = (ends[1] - starts[1]).total_seconds()

    ball = read_ball(next(dfl_dir.glob(f"DFL_04_03_positions_raw_observed_*_{match}.xml")))
    frames = ball[1] + ball[2]
    times = [f[0] for f in frames]
    print(f"{match} ball frames: P1 {len(ball[1])}, P2 {len(ball[2])}; anchor events: {len(anchors)}")

    # Step 1: pick the transform and time offset with the smallest median distance.
    results = {}
    for name, fn in TRANSFORMS.items():
        for off in OFFSETS:
            dists = []
            for et, ex, ey in anchors:
                f = frames[nearest(times, et - off)]
                if abs(f[0] - (et - off)) > 0.1:
                    continue  # no ball frame at this time (between halves)
                bx, by = fn(f[1], f[2])
                dists.append(((bx - ex) ** 2 + (by - ey) ** 2) ** 0.5)
            results[(name, off)] = statistics.median(dists)
    for name in TRANSFORMS:
        best_off = min(OFFSETS, key=lambda o: results[(name, o)])
        print(f"{name}: median {results[(name, 0.0)]:.2f} m at offset 0; best {results[(name, best_off)]:.2f} m at {best_off:+.2f} s")
    (transform, offset), best = min(results.items(), key=lambda kv: kv[1])
    print(f"chosen: {transform}, offset {offset:+.2f} s, median {best:.2f} m")
    if best > max_median_m:
        raise SystemExit(f"{match}: best median {best:.2f} m is above {max_median_m} m")
    fn = TRANSFORMS[transform]

    # Which team does BallPossession 1 mean? Check it against the team making each open-play pass.
    agree = {"1=home": 0, "1=away": 0}
    for et, team in passes:
        f = frames[nearest(times, et - offset)]
        if abs(f[0] - (et - offset)) > 0.1 or f[4] not in (1, 2):
            continue
        is_home = team == home_id
        agree["1=home"] += (f[4] == 1) == is_home
        agree["1=away"] += (f[4] == 1) != is_home
    checked = sum(agree.values())
    one_is_home = agree["1=home"] >= agree["1=away"]
    share = max(agree.values()) / checked
    print(f"BallPossession check over {checked} open-play passes: 1=home agrees {agree['1=home']}, 1=away agrees {agree['1=away']}")
    if share < min_agree:
        raise SystemExit(f"{match}: possession mapping agrees on only {share:.0%} of passes")

    def app_frame(X: float, Y: float, period: int) -> tuple[float, float]:
        mx, my = fn(X, Y)
        x, y = mx / 105 * 100, my / 68 * 100
        if attacks_left[period] == home_id:
            x, y = 100 - x, 100 - y
        return round(x, 1), round(y, 1)

    # Step 2: 5 Hz samples between each half's KickOff and FinalWhistle.
    samples = {}
    for period in (1, 2):
        s, e = starts[period].timestamp(), ends[period].timestamp()
        inside = [f for f in ball[period] if s <= f[0] + offset <= e]
        rows = []
        for f in inside[::FRAME_STEP]:
            x, y = app_frame(f[1], f[2], period)
            poss = 0 if f[4] not in (1, 2) else (1 if (f[4] == 1) == one_is_home else 2)
            rows.append([f[0] + offset - s, x, y, 1 if f[3] == 1 else 0, poss])
        samples[period] = rows

    def ball_at(t: datetime) -> tuple[float, float] | None:
        period = next((p for p in (1, 2) if starts[p] <= t <= ends[p]), None)
        if period is None:
            return None
        f = frames[nearest(times, t.timestamp() - offset)]
        return app_frame(f[1], f[2], period)

    return {"match": match, "homeId": home_id, "awayId": away_id, "starts": starts, "ends": ends,
            "attacksLeft": attacks_left, "firstHalf": first_half, "transform": transform, "offset": offset,
            "medianM": best, "oneIsHome": one_is_home, "checked": checked, "agreement": share,
            "goals": goals, "ballAt": ball_at, "samples": samples}


def build(dfl_dir: Path, match: str, slug: str, root: Path) -> None:
    track = extract(dfl_dir, match)
    starts, first_half = track["starts"], track["firstHalf"]
    transform, offset, one_is_home = track["transform"], track["offset"], track["oneIsHome"]
    checked, share, home_id = track["checked"], track["agreement"], track["homeId"]

    # Step 3: the demo timeline.
    samples = []
    for period in (1, 2):
        for elapsed, x, y, live, poss in track["samples"][period]:
            t = PRE_MATCH + elapsed + (0 if period == 1 else first_half + HALF_BREAK)
            samples.append([round(t, 2), x, y, live, poss])

    out = {"slug": slug, "hz": HZ, "source": "Elite Soccer Dataset (Bassek et al., Scientific Data, 2025), CC BY 4.0",
           "transform": f"{transform} (metres), then percent, home attacks right", "timeOffsetSec": offset,
           "possession": f"BallPossession {'1' if one_is_home else '2'} = home, checked on {checked} passes ({share:.1%} agree)",
           "samples": samples}
    path = root / "data" / "demo" / f"{slug}_ball.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, separators=(",", ":")))

    # Sanity checks.
    live = sum(s[3] for s in samples) / len(samples)
    print(f"{len(samples)} samples, {path.stat().st_size / 1024:.0f} KiB, live {live:.1%}")
    ball_at = track["ballAt"]
    for p in (1, 2):
        print(f"kick-off P{p}: ball {ball_at(starts[p])}")
    for t, team in track["goals"]:
        p = 1 if t <= track["ends"][1] else 2
        el = (t - starts[p]).total_seconds()
        side = "home" if team == home_id else "away"
        print(f"goal {int(el // 60) + 45 * (p - 1)}:{int(el % 60):02d} {side}: ball {ball_at(t)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dfl-dir", required=True, type=Path)
    ap.add_argument("--match", default="DFL-MAT-J03WMX")
    ap.add_argument("--slug", default="j03wmx")
    args = ap.parse_args()
    build(args.dfl_dir, args.match, args.slug, Path(__file__).resolve().parent.parent)


if __name__ == "__main__":
    main()

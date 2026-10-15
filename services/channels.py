import asyncio
import logging
import re
import time

from services.p2proulette import P2PRoulette
from services.balltrack import BallTrack
from services.demo import DemoFeed
from services.feed import FixtureFeed
from services.hub import Hub
from services.match_state import MatchState
from services.penalty import PenaltyPot
from services.wallet import Wallets
from services.www import WhoWhereWhen

log = logging.getLogger(__name__)

MAX_CHANNELS = 8
IDLE_SECONDS = 60
PUBLIC_ROOM = "public"
ROOM_ID = re.compile(r"^(public|[a-z0-9]{6})$")
MAX_ROOMS = 50


class RoomLimit(Exception):
    pass


class Room:
    """One table on a match: its own roulette, Who Where When and penalty pots."""

    def __init__(self, room_id: str, channel: "Channel", loop: asyncio.AbstractEventLoop, wallets: Wallets):
        self.id = room_id
        self.roulette = P2PRoulette(schedule=loop.call_later, wallets=wallets, can_run=channel.ball_in_play_time,
                                    match_time=channel.match_seconds, source=channel.track.source, room=room_id)
        self.www = WhoWhereWhen(schedule=loop.call_later, wallets=wallets, phase=channel.game_phase,
                                squads=channel.squads)
        self.penalty = PenaltyPot(schedule=loop.call_later, wallets=wallets, track=channel.track,
                                  phase=channel.game_phase, room=room_id)
        self.idle_since = time.monotonic()
        self.roulette.start()
        self.penalty.start()

    @property
    def viewers(self) -> int:
        return max(self.roulette.viewers, self.www.viewers, self.penalty.viewers)

    def active(self) -> bool:
        return self.roulette.active() or self.www.active() or self.penalty.active()

    def close(self) -> None:
        self.roulette.stop()
        self.penalty.stop()


class Channel:
    """One fixture: its feed, match state, client hub, ball track and rooms."""

    def __init__(self, fixture_id: str, loop: asyncio.AbstractEventLoop, kafka: tuple[dict, str, str],
                 wallets: Wallets, demo: dict | None = None):
        self.fixture_id = fixture_id
        self.state = MatchState(fixture_id)
        self.hub = Hub()
        self.phase = "pre"  # cached from each summary, so a game never rebuilds match state
        self.is_demo = demo is not None
        # The demo has a real ball track; a live match has only Opta events, so the path is estimated.
        self.track = BallTrack("tracking" if demo and demo.get("ball") else "events")
        self._loop, self._wallets = loop, wallets
        self.rooms: dict[str, Room] = {}
        self.viewers = 0
        self.idle_since = time.monotonic()

        def on_update(changed: list[dict]) -> None:
            summary = self.state.summary()
            self.phase = summary["status"]["phase"]
            loop.call_soon_threadsafe(self.hub.publish, {"type": "update", "events": changed, "summary": summary})
            loop.call_soon_threadsafe(self._on_events, changed, self.phase)

        def on_caught_up() -> None:
            snapshot = self.state.snapshot()
            self.phase = snapshot["status"]["phase"]
            loop.call_soon_threadsafe(self.hub.publish, {"type": "snapshot", "data": snapshot})
            # The feed sends no updates while it reads the match up to now, so the live track starts from the
            # snapshot's recent positioned events.
            loop.call_soon_threadsafe(self._seed_track, snapshot["pitch"])
            # The games read the phase as "loading" until now: tell them it is known.
            loop.call_soon_threadsafe(self._phase_known)

        def on_reset() -> None:
            # A rebuild reads the match again from the start. Bets cannot open until it catches up.
            # A demo jump moves time, so rounds that were running cannot settle fairly.
            self.phase = "pre"
            if demo:
                loop.call_soon_threadsafe(self._void_all, "the demo jumped in time")

        if demo:
            self.feed = DemoFeed(demo["events"], self.state, on_update, on_caught_up, on_reset=on_reset,
                                 ball_path=demo.get("ball"),
                                 on_ball=lambda b: loop.call_soon_threadsafe(self._on_ball, b),
                                 on_status=lambda s: loop.call_soon_threadsafe(self.hub.publish, {"type": "demo", **s}))
        else:
            config, topic, group = kafka
            self.feed = FixtureFeed(config, topic, group, self.state, on_update, on_caught_up, on_reset=on_reset)

    # ---- rooms ----

    def room(self, room_id: str) -> Room:
        """The room, made on first use. Anyone with a private room's link can open it again."""
        room = self.rooms.get(room_id)
        if room is None:
            if len(self.rooms) >= MAX_ROOMS:
                raise RoomLimit(f"at most {MAX_ROOMS} rooms can be open on one match")
            room = self.rooms[room_id] = Room(room_id, self, self._loop, self._wallets)
        # A client is joining it now, so the sweep must not drop it before the client connects.
        room.idle_since = time.monotonic()
        return room

    def sweep_rooms(self) -> None:
        now = time.monotonic()
        for room_id, room in list(self.rooms.items()):
            if room.viewers:
                room.idle_since = now
            elif room_id != PUBLIC_ROOM and now - room.idle_since > IDLE_SECONDS and not room.active():
                room.close()
                del self.rooms[room_id]

    def active(self) -> bool:
        return any(r.active() for r in self.rooms.values())

    def close_rooms(self) -> None:
        for room in self.rooms.values():
            room.close()
        self.rooms.clear()

    def _void_all(self, reason: str) -> None:
        for room in self.rooms.values():
            room.roulette.void_all(reason)
            room.www.void_all(reason)
            room.penalty.void_all(reason)

    # ---- feed fan-out (event loop) ----

    def _on_events(self, changed: list[dict], phase: str) -> None:
        if self.track.source == "events":
            self.track.add_events(changed)
            # Live: clients draw the ball on the same estimated path the rounds settle on.
            self.hub.publish(self.track_message())
        for room in self.rooms.values():
            room.roulette.on_events(changed)
            room.www.on_events(changed)
            room.www.on_phase(phase)
            room.penalty.on_events(changed)
            room.penalty.on_phase(phase)

    def _phase_known(self) -> None:
        for room in self.rooms.values():
            room.www.on_phase(self.game_phase())
            room.penalty.on_phase(self.game_phase())

    def _seed_track(self, events: list[dict]) -> None:
        if self.track.source == "events":
            self.track.add_events(events)
            self.hub.publish(self.track_message())

    def track_message(self) -> dict:
        return {"type": "track", "source": self.track.source, "serverTime": time.time(),
                "points": self.track.recent_json(time.time())}

    def _on_ball(self, ball: dict) -> None:
        self.hub.publish({"type": "ball", **ball})
        self.track.add_tracking(time.time(), ball["x"], ball["y"], bool(ball["live"]))

    def game_phase(self) -> str:
        """The phase the games may act on. While the feed reads the match up to now, it is "loading": the
        cached phase can still say "pre" for a match that kicked off long ago."""
        return self.phase if self.feed.caught_up else "loading"

    def ball_in_play_time(self) -> bool:
        """Rounds run while a half is in play and the demo clock is not paused."""
        return self.game_phase() == "live" and self.state.clock_rate != 0

    def match_seconds(self) -> float | None:
        """Match-clock seconds now while a half is in play (demo speed and pauses included); else None. None
        too once the clock holds: the feed has sent nothing for holdSec, so no play is known."""
        summary = self.state.summary()
        status, clock = summary["status"], summary["clock"]
        if status["phase"] != "live" or not clock or clock["periodId"] != status["periodId"]:
            return None
        run = max(0.0, time.time() - clock["seenAt"]) * (clock.get("rate") or 0)
        if run >= clock["holdSec"]:
            return None
        return clock["min"] * 60 + clock["sec"] + run

    def squads(self) -> dict[str, set[str]]:
        teams = self.state.summary()["teams"]
        return {side: set(t["starters"]) | set(t["subs"]) for side, t in teams.items()}

    def refresh_status(self) -> None:
        """Recompute the phase with no new message, so a feed that goes quiet still ends the match."""
        summary = self.state.summary()
        # A message applied meanwhile has set a newer phase from the feed thread.
        if summary["seq"] == self.state.messages_applied and summary["status"]["phase"] != self.phase:
            self.phase = summary["status"]["phase"]
            self.hub.publish({"type": "update", "events": [], "summary": summary})
            for room in self.rooms.values():
                room.www.on_phase(self.phase)
                room.penalty.on_phase(self.phase)

    def health(self) -> dict:
        return {"fixtureId": self.fixture_id, "viewers": self.viewers, "rooms": len(self.rooms),
                "clients": self.hub.client_count, "ballSource": self.track.source, **self.feed.health()}


class ChannelLimit(Exception):
    pass


class Channels:
    """Starts a channel when the first viewer arrives and stops it after IDLE_SECONDS with none.
    Only touch it from the event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, kafka: tuple[dict, str, str], wallets: Wallets,
                 demos: dict[str, dict] | None = None):
        self._loop, self._kafka, self._wallets = loop, kafka, wallets
        self.demos = demos or {}
        self._channels: dict[str, Channel] = {}
        self._sweeper: asyncio.Task | None = None

    def start(self) -> None:
        self._sweeper = asyncio.create_task(self._sweep())

    def acquire(self, fixture_id: str) -> Channel:
        channel = self._channels.get(fixture_id)
        if channel is None:
            if len(self._channels) >= MAX_CHANNELS:
                raise ChannelLimit(f"at most {MAX_CHANNELS} matches can be open at once")
            channel = self._channels[fixture_id] = Channel(fixture_id, self._loop, self._kafka, self._wallets,
                                                           demo=self.demos.get(fixture_id))
            channel.feed.start()
            log.info("opened channel for %s", fixture_id)
        channel.viewers += 1
        return channel

    def release(self, channel: Channel) -> None:
        channel.viewers = max(0, channel.viewers - 1)
        if channel.viewers == 0:
            channel.idle_since = time.monotonic()

    def get(self, fixture_id: str) -> Channel | None:
        return self._channels.get(fixture_id)

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            for key, channel in list(self._channels.items()):
                channel.refresh_status()
                channel.sweep_rooms()
                if channel.viewers == 0 and now - channel.idle_since > IDLE_SECONDS and not channel.active():
                    channel.close_rooms()
                    del self._channels[key]
                    log.info("closing idle channel for %s", channel.fixture_id)
                    # stop() joins the feed thread, so keep it off the event loop.
                    await self._loop.run_in_executor(None, channel.feed.stop)

    def health(self) -> list[dict]:
        return [channel.health() for channel in self._channels.values()]

    async def close(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
        for c in self._channels.values():
            c.close_rooms()
        await asyncio.gather(*(self._loop.run_in_executor(None, c.feed.stop) for c in self._channels.values()))
        self._channels.clear()

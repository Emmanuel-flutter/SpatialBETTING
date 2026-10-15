import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import (CONFLUENT_TOPIC, CONSUMER_GROUP, DEMO_MATCHES, FIXTURE_ID, KAFKA_CONSUMER_CONFIG, LOG_LEVEL,
                    ROULETTE_START_BALANCE)
from services.channels import PUBLIC_ROOM, ROOM_ID, ChannelLimit, Channels, RoomLimit
from services.fixtures import FixtureIndex
from services.wallet import Wallets

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("spatialbetting")
PLAYER_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
FIXTURE = re.compile(r"^([a-z0-9]{10,40}|demo-[a-z0-9]{2,20})$")
MESSAGES_PER_SECOND = 10


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    kafka = (KAFKA_CONSUMER_CONFIG, CONFLUENT_TOPIC, CONSUMER_GROUP)
    index = FixtureIndex(*kafka)
    channels = Channels(loop, kafka, Wallets(ROULETTE_START_BALANCE), DEMO_MATCHES)
    app.state.index, app.state.channels = index, channels
    index.start()
    channels.start()
    yield
    await channels.close()
    await loop.run_in_executor(None, index.stop)


app = FastAPI(title="SpatialBETTING", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def revalidate_pages(request, call_next):
    # The UI changes often during the prototype. no-cache makes the browser check the ETag on every
    # load, so an edited file shows on a normal refresh instead of a stale cached copy.
    response = await call_next(request)
    if request.url.path == "/" or request.url.path == "/match" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
async def lobby() -> FileResponse:
    return FileResponse("static/lobby.html")


@app.get("/match", include_in_schema=False)
async def match_page() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/api/fixtures")
async def fixtures() -> dict:
    index: FixtureIndex = app.state.index
    return {"caughtUp": index.caught_up, "serverTime": time.time(), "defaultFixtureId": FIXTURE_ID,
            "fixtures": index.list()}


@app.get("/api/match")
async def match(fixture: str = FIXTURE_ID) -> JSONResponse:
    channel = app.state.channels.get(fixture)
    if channel is None:
        return JSONResponse({"error": "that match is not open; open it at /match first"}, status_code=404)
    return JSONResponse(channel.state.snapshot())


@app.get("/healthz")
async def healthz() -> JSONResponse:
    index = app.state.index.health()
    health = {"index": index, "channels": app.state.channels.health()}
    return JSONResponse(health, status_code=200 if index["connected"] else 503)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    player_id = websocket.query_params.get("player", "")
    fixture_id = websocket.query_params.get("fixture") or FIXTURE_ID
    room_id = websocket.query_params.get("room") or PUBLIC_ROOM
    # Accept before any rejection: a close before accept reaches the browser as an HTTP 403 (code 1006).
    await websocket.accept()
    if not PLAYER_ID.match(player_id) or not FIXTURE.match(fixture_id):
        await websocket.close(code=1008, reason="unknown match")
        return
    if not ROOM_ID.match(room_id):
        await websocket.close(code=1008, reason="unknown room")
        return
    index: FixtureIndex = app.state.index
    known = index.get(fixture_id)
    if fixture_id.startswith("demo-") and fixture_id not in app.state.channels.demos:
        await websocket.close(code=1008, reason="unknown match")
        return
    if known is None and index.caught_up and not fixture_id.startswith("demo-"):
        await websocket.close(code=1008, reason="unknown match")
        return
    channels: Channels = app.state.channels
    try:
        channel = channels.acquire(fixture_id)
    except ChannelLimit:
        await websocket.close(code=1013, reason="too many matches open")
        return
    try:
        room = channel.room(room_id)
    except RoomLimit:
        channels.release(channel)
        await websocket.close(code=1013, reason="too many rooms open")
        return
    hub = channel.hub
    # Subscribe before the snapshot, so no update falls between the two. The client upserts by
    # event id and seqId, so an update already in the snapshot is harmless.
    queue = hub.subscribe()
    # Wait on the socket as well as the queue. Otherwise a closed client (or a server shutdown) is
    # only noticed on the next send, and a quiet feed keeps the handler, and shutdown, hanging.
    receiver = asyncio.create_task(websocket.receive())

    def send_game(message: dict) -> None:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            # A missed balance or result would leave the tab wrong, so force a fresh snapshot instead.
            log.warning("client queue full, forcing resync")
            queue.get_nowait()
            queue.put_nowait({"type": "resync"})

    allowance, refilled = float(MESSAGES_PER_SECOND), time.monotonic()
    getter: asyncio.Task | None = None
    try:
        await websocket.send_json({"type": "snapshot", "data": channel.state.snapshot()})
        if channel.track.source == "events":
            await websocket.send_json(channel.track_message())
        room.roulette.connect(player_id, send_game)
        room.www.connect(player_id, send_game)
        room.penalty.connect(player_id, send_game)
        while True:
            getter = getter or asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                message = getter.result()
                getter = None
                await websocket.send_json(message)
                if message["type"] == "resync":
                    await websocket.close(code=1013)
                    return
            if receiver in done:
                incoming = receiver.result()
                if incoming["type"] == "websocket.disconnect":
                    return
                now = time.monotonic()
                allowance = min(MESSAGES_PER_SECOND, allowance + (now - refilled) * MESSAGES_PER_SECOND)
                refilled = now
                if allowance >= 1:
                    allowance -= 1
                    handle_client_message(channel, room, player_id, incoming.get("text"))
                receiver = asyncio.create_task(websocket.receive())
    except WebSocketDisconnect:
        pass
    finally:
        receiver.cancel()
        if getter:
            getter.cancel()
        room.roulette.disconnect(player_id, send_game)
        room.www.disconnect(player_id, send_game)
        room.penalty.disconnect(player_id, send_game)
        hub.unsubscribe(queue)
        channels.release(channel)


def handle_client_message(channel, room, player_id: str, text: str | None) -> None:
    if not text or len(text) > 4096:  # a full betslip is a few kB
        return
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(message, dict):
        return
    kind = str(message.get("type", ""))
    if kind == "demo_control":
        # Demo only: pause, speed, rewind and skip. Every viewer of the demo shares one clock.
        if channel.is_demo:
            channel.feed.control(str(message.get("action", "")), message.get("value"))
        return
    if kind.startswith("www_"):
        room.www.handle(player_id, message)
    elif kind.startswith("br_"):
        room.roulette.handle(player_id, message)
    elif kind.startswith("pen_"):
        room.penalty.handle(player_id, message)

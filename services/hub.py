import asyncio
import logging

log = logging.getLogger(__name__)


class Hub:
    """Fan-out of feed updates to WebSocket clients. Only touch it from the event loop."""

    def __init__(self, queue_size: int = 512):
        self._queue_size = queue_size
        self._clients: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    def publish(self, message: dict) -> None:
        for q in list(self._clients):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # A slow client gets a reset marker; it reconnects and takes a fresh snapshot.
                log.warning("client queue full, forcing resync")
                self._clients.discard(q)
                q.get_nowait()
                q.put_nowait({"type": "resync"})

    @property
    def client_count(self) -> int:
        return len(self._clients)

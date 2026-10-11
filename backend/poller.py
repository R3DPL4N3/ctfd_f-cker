"""Background CTFd poller — detects new and solved challenges every 5 seconds."""

import asyncio
import logging
from dataclasses import dataclass, field

from backend.ctfd import CTFdClient

logger = logging.getLogger(__name__)


@dataclass
class PollEvent:
    kind: str  # "new_challenge" | "challenge_solved"
    challenge_name: str
    details: dict = field(default_factory=dict)


@dataclass
class CTFdPoller:
    """Polls CTFd every interval_s seconds, emits events for new/solved challenges."""

    ctfd: CTFdClient
    interval_s: float = 5.0

    _known_challenges: set[str] = field(default_factory=set)
    _known_solved: set[str] = field(default_factory=set)
    _ids_by_name: dict[str, int] = field(default_factory=dict)
    _event_queue: asyncio.Queue[PollEvent] = field(default_factory=asyncio.Queue)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _poll_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> None:
        """Do initial poll (silent — no events) and start the background loop."""
        await self._seed()
        logger.info(
            "Poller initialized: %d challenges, %d solved",
            len(self._known_challenges),
            len(self._known_solved),
        )
        self._task = asyncio.create_task(self._loop(), name="ctfd-poller")

    async def _seed(self) -> None:
        """Initial fetch — just populate known state, no events."""
        try:
            stubs = await self.ctfd.fetch_challenge_stubs()
            self._remember_stubs(stubs)
            self._known_solved = await self.ctfd.fetch_solved_names()
        except Exception as e:
            logger.warning("Initial poll error: %s", e)

    async def refresh(self) -> list[PollEvent]:
        """Poll immediately and return new events without queueing them.

        The caller routes these events before the normal auto-spawn path sees them.
        """
        async with self._poll_lock:
            return await self._collect_events(enqueue=False)

    def challenge_id(self, name: str) -> int | None:
        return self._ids_by_name.get(name)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def get_event(self, timeout: float = 1.0) -> PollEvent | None:
        """Non-blocking get — returns None if no event within timeout."""
        try:
            return await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError):
            return None

    def drain_events(self) -> list[PollEvent]:
        """Drain all pending events without blocking."""
        events: list[PollEvent] = []
        while not self._event_queue.empty():
            try:
                events.append(self._event_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    @property
    def known_challenges(self) -> set[str]:
        return set(self._known_challenges)

    @property
    def known_solved(self) -> set[str]:
        return set(self._known_solved)

    def _remember_stubs(self, stubs: list[dict]) -> None:
        visible = [ch for ch in stubs if ch.get("name")]
        self._known_challenges = {ch["name"] for ch in visible}
        for challenge in visible:
            if challenge.get("id") is not None:
                self._ids_by_name[challenge["name"]] = challenge["id"]

    async def _collect_events(self, *, enqueue: bool) -> list[PollEvent]:
        try:
            stubs = await self.ctfd.fetch_challenge_stubs()
            visible = [ch for ch in stubs if ch.get("name")]
            current_names = {ch["name"] for ch in visible}
            stubs_by_name = {ch["name"]: ch for ch in visible}
            current_solved = await self.ctfd.fetch_solved_names()

            # Sanity check: if results look bogus compared to what we know, skip.
            if self._known_challenges and len(current_names) < len(self._known_challenges) // 2:
                logger.warning(
                    "Poll returned suspicious data (%d challenges vs %d known) — skipping",
                    len(current_names),
                    len(self._known_challenges),
                )
                return []
            # Don't let solved count regress (API might return empty on errors)
            if self._known_solved and not current_solved:
                logger.warning("Poll returned 0 solved (had %d) — skipping", len(self._known_solved))
                return []

            events: list[PollEvent] = []
            # This milestone treats unlock as "was hidden, now visible".
            # TODO: also detect locked/anonymized → unlocked when CTFd exposes
            # that state on already-visible stubs.
            for name in sorted(current_names - self._known_challenges):
                stub = stubs_by_name.get(name) or {}
                logger.info("New challenge detected: %s", name)
                events.append(PollEvent(
                    "new_challenge",
                    name,
                    {"id": stub.get("id"), "category": stub.get("category") or ""},
                ))

            for name in sorted(current_solved - self._known_solved):
                logger.info("Challenge solved: %s", name)
                events.append(PollEvent(
                    "challenge_solved",
                    name,
                    {"id": self._ids_by_name.get(name)},
                ))

            self._remember_stubs(visible)
            self._known_solved = current_solved
            if enqueue:
                for event in events:
                    self._event_queue.put_nowait(event)
            return events
        except Exception as e:
            logger.warning("Poll error: %s", e)
            return []

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.interval_s)
            async with self._poll_lock:
                await self._collect_events(enqueue=True)

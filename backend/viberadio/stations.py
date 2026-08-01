"""Runs each station's agents on demand.

Three stations running continuously would triple the LLM calls, TTS work and
downloads regardless of whether anyone is listening. Instead a station spins up
the first time someone asks for it and shuts down again once nobody has asked
for a while — so tuning to a cold station costs a wait, not a standing bill.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from .config import settings
from .db import session_scope
from .models import Channel

log = logging.getLogger("stations")

REAP_INTERVAL_SEC = 30.0


@dataclass
class Runner:
    channel_id: int
    slug: str
    agents: list = field(default_factory=list)
    started_at: float = 0.0
    last_seen: float = 0.0


class StationManager:
    def __init__(self) -> None:
        self._runners: dict[int, Runner] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    # ---- lifecycle -------------------------------------------------------

    def start_reaper(self) -> None:
        self._reaper = asyncio.create_task(self._reap_loop(), name="station-reaper")

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
        async with self._lock:
            runners = list(self._runners.values())
            self._runners.clear()
        for runner in runners:
            await self._stop(runner)

    # ---- demand ----------------------------------------------------------

    async def touch(self, channel: Channel) -> None:
        """Register interest in a station, starting its agents if they're not up."""
        async with self._lock:
            runner = self._runners.get(channel.id)
            now = asyncio.get_running_loop().time()
            if runner is None:
                runner = await self._start(channel, now)
            runner.last_seen = now

    def is_running(self, channel_id: int) -> bool:
        return channel_id in self._runners

    def running_slugs(self) -> list[str]:
        return [r.slug for r in self._runners.values()]

    def status(self, channel_id: int, has_programme: bool) -> str:
        """`off` (idle), `starting` or `live`.

        `live` means real programme — a song or DJ break on the schedule. A
        freshly-woken station starts streaming its holding jingle within seconds,
        long before the selector has chosen anything, and calling that "live"
        would tell the listener the studio is simply empty.
        """
        if channel_id not in self._runners:
            return "off"
        return "live" if has_programme else "starting"

    # ---- internals -------------------------------------------------------

    async def _start(self, channel: Channel, now: float) -> Runner:
        from .agents.engineer import Engineer
        from .agents.news_agent import News
        from .agents.selector import Selector
        from .agents.voice import Voice

        settings.hls_dir_for(channel.slug).mkdir(parents=True, exist_ok=True)
        agents = [
            Engineer(channel.id, channel.slug),
            Selector(channel.id, channel.slug),
            Voice(channel.id, channel.slug, channel.tts_voice),
            # Writes news copy into the database for this station. Nothing reads it
            # back yet — it is not on the playlist and not in the mix.
            News(channel.id, channel.slug),
        ]
        for agent in agents:
            agent.start()
        runner = Runner(
            channel_id=channel.id,
            slug=channel.slug,
            agents=agents,
            started_at=now,
            last_seen=now,
        )
        self._runners[channel.id] = runner
        log.info("station %s spinning up", channel.slug)
        return runner

    async def _stop(self, runner: Runner) -> None:
        """Stop a runner that has already been removed from the registry.

        Never call this while holding `_lock`: an agent parked in a download or
        an LLM call can take tens of seconds to unwind, and every station
        request would queue behind it.
        """
        for agent in runner.agents:
            await agent.stop()
        log.info("station %s idle — shut down", runner.slug)

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(REAP_INTERVAL_SEC)
                cutoff = (
                    asyncio.get_running_loop().time()
                    - settings.station_idle_timeout_sec
                )
                # Deregister under the lock, shut down outside it. A station is
                # "off" the moment it leaves the registry, so a slow teardown
                # can't block a listener tuning to a different one.
                async with self._lock:
                    expired = [
                        r for r in self._runners.values() if r.last_seen < cutoff
                    ]
                    for runner in expired:
                        self._runners.pop(runner.channel_id, None)
                for runner in expired:
                    await self._stop(runner)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("station reaper tick failed")


manager = StationManager()


async def get_channel(slug: str | None) -> Channel | None:
    """Look a station up by slug, defaulting to the first on the dial."""
    async with session_scope() as session:
        q = select(Channel).order_by(Channel.sort_order, Channel.id)
        if slug:
            q = q.where(Channel.slug == slug)
        return await session.scalar(q.limit(1))

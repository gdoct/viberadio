import asyncio
import logging


class AgentLoop:
    """Async background loop: runs `tick()` every `interval` seconds, or sooner when
    `wake` is set. Exceptions are logged and backed off — the loop never dies."""

    def __init__(self, name: str, interval: float, wake: asyncio.Event | None = None):
        self.name = name
        self.interval = interval
        self.wake = wake
        self.log = logging.getLogger(f"agent.{name}")
        self._task: asyncio.Task | None = None

    async def tick(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _run(self) -> None:
        self.log.info("started")
        backoff = 1.0
        while True:
            try:
                await self.tick()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("tick failed; backing off %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            try:
                if self.wake is not None:
                    await asyncio.wait_for(self.wake.wait(), timeout=self.interval)
                    self.wake.clear()
                else:
                    await asyncio.sleep(self.interval)
            except TimeoutError:
                pass

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"agent-{self.name}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self.log.info("stopped")

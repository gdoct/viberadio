import asyncio

# Wake events shared between agents and the API layer, one pair per channel — a
# request on one station must not spin the other stations' agents. Created lazily
# so importing this module doesn't require a running event loop.
_selector_events: dict[int, asyncio.Event] = {}
_voice_events: dict[int, asyncio.Event] = {}


def selector_event(channel_id: int) -> asyncio.Event:
    return _selector_events.setdefault(channel_id, asyncio.Event())


def voice_event(channel_id: int) -> asyncio.Event:
    return _voice_events.setdefault(channel_id, asyncio.Event())


def wake_selector(channel_id: int) -> None:
    selector_event(channel_id).set()


def wake_voice(channel_id: int) -> None:
    voice_event(channel_id).set()

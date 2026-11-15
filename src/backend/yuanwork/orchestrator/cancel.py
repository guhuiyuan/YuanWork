"""Cancelling a generation that is already running.

A user who changes their mind about a question should not have to wait
for the answer to finish. The registry here is how the cancel endpoint
reaches the run that is in progress: the run registers an event when it
starts, checks it between steps and between tokens, and stops as soon
as the event is set.

The registry lives in this process. A request that is cancelled after
the process died has nothing left to cancel anyway.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger(__name__)


class Cancelled(Exception):
    """The run was stopped on purpose."""


#: The events of the runs that are in progress, by session.
_events: dict[str, asyncio.Event] = {}


def start(session_id: str) -> asyncio.Event:
    """Register a run and hand back the event that stops it."""
    event = asyncio.Event()
    _events[session_id] = event
    return event


def cancel(session_id: str) -> bool:
    """Ask the run of this session to stop.

    Returns whether there was a run to stop.
    """
    event = _events.get(session_id)
    if event is None:
        return False
    event.set()
    logger.info(f"The run of session {session_id} was cancelled")
    return True


def finish(session_id: str) -> None:
    """The run is over - forget its event."""
    _events.pop(session_id, None)


def is_cancelled(session_id: str) -> bool:
    """Whether the run of this session was asked to stop."""
    event = _events.get(session_id)
    return bool(event and event.is_set())


def running_sessions() -> list[str]:
    """The sessions that have a run in progress."""
    return list(_events)


async def run_until_cancelled(
    coro: Coroutine[Any, Any, Any], event: asyncio.Event
) -> Any:
    """Run a coroutine until it finishes or the run is cancelled.

    The blocking answer step is one call that cannot be interrupted from
    the inside - so it runs as a task and is cancelled from the outside
    when the user asks for it.
    """
    task = asyncio.ensure_future(coro)
    while True:
        done, _pending = await asyncio.wait({task}, timeout=0.1)
        if done:
            return task.result()
        if event.is_set():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise Cancelled()

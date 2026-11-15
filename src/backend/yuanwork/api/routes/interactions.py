"""The interactions the user has with an answer or a document.

Three of the six interactions happen on the client: the user opens a
citation, accepts an answer or rejects it. The other three - asking,
adding a file, removing a file - happen where the action does, and are
recorded there.

The endpoint records what the client reports. It does not ask the
client to be trusted about much: the kind is one of three, everything
else is text that ends up in the local event log.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter

from yuanwork.api.deps import optional_episodic_recorder
from yuanwork.api.schemas import InteractionRequest
from yuanwork.memory.episodic_recorder import EpisodicEvent

logger = logging.getLogger(__name__)

router = APIRouter()


#: The interactions the client reports.
InteractionKind = Literal["cite", "accept", "reject"]


@router.post("/interactions/{kind}")
async def record_interaction(
    kind: InteractionKind, body: InteractionRequest
) -> dict:
    """Record one interaction the user had with an answer or a document."""
    recorder = optional_episodic_recorder()
    if recorder is None:
        # The memory engineering layer is switched off - the interaction
        # still happened, it just is not written down.
        return {"recorded": False, "reason": "episodic memory disabled"}

    event_id = await recorder.record(
        EpisodicEvent(
            kind=kind,
            session_id=body.session_id or "unknown",
            subject=body.subject,
            detail=body.detail,
            topic=body.topic,
            importance=body.importance,
        )
    )

    return {"recorded": bool(event_id), "id": event_id}

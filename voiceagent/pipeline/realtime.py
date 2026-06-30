"""RealtimePipeline: single-socket Realtime API pipeline (interface stub).

This milestone ships the assembled pipeline fully and a conforming stub for the unified Realtime
path (OpenAI Realtime / Gemini Live), selectable via agent.yaml `pipeline.mode: realtime`. The
stub satisfies the Pipeline Protocol so the session loop and config selection are exercised now;
the socket wiring is deferred. Constructing or starting it raises a clear NotImplementedError so
a user who selects realtime before it is built gets an actionable message, not a silent failure.
"""

from __future__ import annotations

from typing import AsyncIterator, Optional

from ..contracts import CostMeter, MediaStream, STTEvent, Utterance


class RealtimePipeline:
    """Conforms to the Pipeline Protocol. Not yet wired to a provider socket."""

    def __init__(self, api_key: str = "", model: str = "", cost_meter: Optional[CostMeter] = None):
        self.api_key = api_key
        self.model = model
        self.cost_meter = cost_meter

    async def start(self, media: MediaStream) -> None:
        raise NotImplementedError(
            "pipeline.mode 'realtime' is not implemented yet. Use 'assembled' "
            "(Deepgram + Claude + ElevenLabs), or implement the Realtime socket here."
        )

    def user_events(self) -> AsyncIterator[STTEvent]:  # pragma: no cover - stub
        raise NotImplementedError

    async def say(self, text_stream: AsyncIterator[str]) -> Utterance:  # pragma: no cover - stub
        raise NotImplementedError

    async def barge_in(self) -> None:  # pragma: no cover - stub
        raise NotImplementedError

    async def close(self) -> None:
        return None

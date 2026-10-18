"""Regression tests for the live WebSocket connect path.

The provider unit tests cover pure parsing, but the first live call failed because
DeepgramSTT.connect() called websockets.connect(additional_headers=...), which is the v13+ name
and raises TypeError on the pinned websockets 12.x. These tests drive connect() with a fake
websockets module that mimics v12 (accepts extra_headers, rejects additional_headers), so the
kwarg mismatch can never silently come back.
"""

from __future__ import annotations

import sys
import types


class _FakeV12WS:
    async def send(self, *a, **k):
        pass

    async def close(self, *a, **k):
        pass

    async def recv(self):
        import asyncio

        await asyncio.sleep(3600)  # block; tests cancel/close before this matters

    def __aiter__(self):
        return self

    async def __anext__(self):
        import asyncio

        await asyncio.sleep(3600)
        raise StopAsyncIteration


def _install_fake_websockets(monkeypatch, captured):
    async def fake_connect(url, **kwargs):
        captured["kwargs"] = kwargs
        if "additional_headers" in kwargs:
            raise TypeError(
                "create_connection() got an unexpected keyword argument 'additional_headers'"
            )
        return _FakeV12WS()

    fake_mod = types.ModuleType("websockets")
    fake_mod.connect = fake_connect
    exc_mod = types.ModuleType("websockets.exceptions")

    class ConnectionClosed(Exception):
        pass

    exc_mod.ConnectionClosed = ConnectionClosed
    fake_mod.exceptions = exc_mod
    monkeypatch.setitem(sys.modules, "websockets", fake_mod)
    monkeypatch.setitem(sys.modules, "websockets.exceptions", exc_mod)


async def test_deepgram_connect_uses_v12_compatible_headers(monkeypatch):
    from voiceagent.stt.deepgram import DeepgramSTT

    captured: dict = {}
    _install_fake_websockets(monkeypatch, captured)

    stt = DeepgramSTT("fake-key", endpointing_ms=300)
    await stt.connect()  # must not raise TypeError
    assert "extra_headers" in captured["kwargs"]
    assert "additional_headers" not in captured["kwargs"]
    await stt.close()

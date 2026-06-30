"""FastAPI server: the Twilio Media Streams WebSocket endpoint plus the demo UI mount.

Twilio connects a bidirectional Media Stream to /stream. Per call we read the start handshake
(which carries the contact info as custom parameters), build a CallSession wired to the real
providers, run it to completion, and log the outcome. The whole handler is guarded so one bad
call never takes down the server.

The demo Web UI (Phase 6) is mounted from voiceagent.web. The core stays headless: the server
runs fine without the UI.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket

from .builder import build_session
from .config import AgentConfig, Settings, load_agent_config, settings_from_env
from .logger import build_outcome_sink
from .telephony.twilio import TwilioMediaStream

DEFAULT_CONFIG = os.getenv("VOICEAGENT_CONFIG", "examples/agent.yaml")


def _configure_logging() -> None:
    """Make voiceagent.* logs (the per-call timeline and handler errors) reach stdout regardless
    of how uvicorn configures logging, so live calls are diagnosable from the server console."""

    vlog = logging.getLogger("voiceagent")
    if not vlog.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        vlog.addHandler(handler)
        vlog.setLevel(logging.INFO)
        vlog.propagate = False


_configure_logging()


def _contact_from_params(params: dict) -> dict:
    """Build a contact dict from Twilio <Parameter> custom parameters (all optional)."""

    return {
        "name": params.get("name", "there"),
        "phone": params.get("phone", ""),
        "email": params.get("email", ""),
        "suggested_time": params.get("suggested_time", "later this week"),
    }


def create_app(config_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="VoiceAgent", version="0.1.0")
    app.state.config_path = config_path or DEFAULT_CONFIG
    app.state.settings = settings_from_env()
    app.state.outcome_sink = build_outcome_sink(csv_path="call_logs/outcomes.csv")

    def _load_config() -> AgentConfig:
        return load_agent_config(app.state.config_path)

    @app.get("/health")
    async def health() -> dict:
        settings: Settings = app.state.settings
        return {
            "status": "ok",
            "missing_for_live_call": settings.missing_for_live_call(),
        }

    @app.websocket("/stream")
    async def stream(websocket: WebSocket) -> None:
        await websocket.accept()
        media = TwilioMediaStream(websocket)
        try:
            # Read the start handshake first so we know who we are talking to.
            params = await media.start_handshake()
            contact = _contact_from_params(params)
            config = _load_config()
            session = build_session(config, app.state.settings, contact, media)
            result = await session.run()
            await app.state.outcome_sink.record(result)
        except Exception:
            # Never let one call crash the server, but do not be blind: log the failure so a
            # handshake or wiring error (before run() can record an outcome) is visible.
            logging.getLogger("voiceagent.server").exception("call handler failed on /stream")
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    # Mount the demo Web UI if present (Phase 6). Import is guarded so the core server runs
    # even if the web package is absent.
    try:
        from .web import mount_web_ui

        mount_web_ui(app)
    except Exception:
        pass

    return app


# Module-level app for `uvicorn voiceagent.server:app`.
app = create_app()


def _ensure_log_dir() -> None:
    Path("call_logs").mkdir(parents=True, exist_ok=True)


_ensure_log_dir()

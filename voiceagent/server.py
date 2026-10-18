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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request, Response, WebSocket

# Imported at startup, never inside a handler: a cold import on the event loop stalls live calls.
from twilio.request_validator import RequestValidator

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
        "timezone": params.get("timezone", ""),
    }


# Twilio AnsweredBy values that mean nobody is listening live.
_MACHINE_ANSWERS = {"machine_end_beep", "machine_end_silence", "machine_end_other", "fax"}


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # SQLite (local dev) gets its tables created on boot. Postgres is migrated by Alembic
    # (`voiceagent db-upgrade`, run by the container entrypoint) before the server starts.
    from .app.db import get_database

    db = get_database()
    if db.engine.url.get_backend_name() == "sqlite":
        await db.create_all()
    # Load phone-number metadata now, in a thread, so the first API request never stalls a call.
    import asyncio

    from . import phones

    await asyncio.to_thread(phones.warm_up)
    yield


def create_app(config_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="VoiceAgent", version="0.1.0", lifespan=_lifespan)
    app.state.config_path = config_path or DEFAULT_CONFIG
    app.state.settings = settings_from_env()
    app.state.outcome_sink = build_outcome_sink(csv_path="call_logs/outcomes.csv")
    # Live sessions by call SID, so carrier callbacks (answering machine detection) reach them.
    app.state.sessions = {}
    app.state.amd_early = {}  # AMD results that arrived before the media stream connected

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
            sid = media.call_sid
            app.state.sessions[sid] = session
            early = app.state.amd_early.pop(sid, None)
            if early:
                session.notify_machine(early)
            try:
                result = await session.run()
            finally:
                app.state.sessions.pop(sid, None)
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

    @app.post("/voice/amd")
    async def amd_callback(request: Request) -> Response:
        """Twilio async answering-machine-detection result for an outbound call."""

        body = (await request.body()).decode("utf-8", "replace")
        form = dict(parse_qsl(body, keep_blank_values=True))
        if not _valid_twilio_request(app.state.settings, request, form):
            return Response(status_code=403)
        sid = form.get("CallSid", "")
        answered_by = form.get("AnsweredBy", "")
        if sid and answered_by in _MACHINE_ANSWERS:
            session = app.state.sessions.get(sid)
            if session is not None:
                session.notify_machine(answered_by)
            else:
                app.state.amd_early[sid] = answered_by
        return Response(status_code=204)

    # Platform JSON API: accounts, workspaces, BYO keys, agents, call history.
    from .app.api import api_router

    app.include_router(api_router)

    # Mount the demo Web UI if present (Phase 6). Import is guarded so the core server runs
    # even if the web package is absent.
    try:
        from .web import mount_web_ui

        mount_web_ui(app)
    except Exception:
        pass

    return app


def _valid_twilio_request(settings: Settings, request: Request, form: dict) -> bool:
    """Check X-Twilio-Signature against the public URL Twilio called. Without an auth token
    (mocked/dev runs) there is nothing to check against, so requests are accepted."""

    token = settings.twilio_auth_token
    if not token:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    url = base + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return RequestValidator(token).validate(url, form, signature)


# Module-level app for `uvicorn voiceagent.server:app`.
app = create_app()


def _ensure_log_dir() -> None:
    Path("call_logs").mkdir(parents=True, exist_ok=True)


_ensure_log_dir()

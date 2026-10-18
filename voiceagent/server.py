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
        "timezone": params.get("timezone", ""),
    }


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
            if params.get("call_id"):
                # Platform call: the workspace's agent, keys and lead, authorized by a signed token.
                await _run_platform_call(app, media, params)
                return
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

    # Platform JSON API (accounts, workspaces, keys, agents, connectors, campaigns) and the
    # Twilio-facing voice routes (inbound, status, answering-machine detection).
    from .app.api import api_router
    from .app.voice import amd_router
    from .app.voice import router as voice_router

    app.include_router(api_router)
    app.include_router(voice_router)
    app.include_router(amd_router(app.state.sessions, app.state.amd_early))

    # Mount the demo Web UI if present (Phase 6). Import is guarded so the core server runs
    # even if the web package is absent.
    try:
        from .web import mount_web_ui

        mount_web_ui(app)
    except Exception:
        pass

    # Last: the dashboard's catch-all route must not shadow anything registered above.
    _mount_dashboard(app)
    return app


# Paths the dashboard's client-side router must never shadow.
_SERVER_PREFIXES = ("api/", "voice/", "stream", "health", "ui", "docs", "openapi.json", "assets/")


def _dashboard_dir() -> Optional[Path]:
    candidates = [os.getenv("VOICEAGENT_WEB_DIST"), Path(__file__).resolve().parent.parent / "web" / "dist"]
    for c in candidates:
        if c and (Path(c) / "index.html").exists():
            return Path(c)
    return None


def _mount_dashboard(app: FastAPI) -> None:
    """Serve the built React dashboard (web/dist) at /, with an SPA fallback so deep links like
    /calls/<id> load index.html. Skipped when the dashboard has not been built (CLI dev mode)."""

    dist = _dashboard_dir()
    if dist is None:
        return
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    index = dist / "index.html"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="dashboard-assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def dashboard(path: str):
        if path.startswith(_SERVER_PREFIXES):
            from fastapi import HTTPException

            raise HTTPException(404)
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)  # favicon.svg and other public files
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


async def _run_platform_call(app: FastAPI, media: TwilioMediaStream, params: dict) -> None:
    from .app.voice import fail_platform_call, load_platform_call, save_platform_result

    log = logging.getLogger("voiceagent.server")
    ctx = await load_platform_call(params)
    if ctx is None:
        return
    try:
        session = build_session(ctx.config, ctx.settings, ctx.contact, media)
    except Exception as exc:  # e.g. a key was deleted between dial and answer
        log.warning("platform call %s could not start: %r", ctx.call_id, exc)
        await fail_platform_call(ctx.call_id, f"could not start: {exc}")
        return
    session.events_dir = f"call_logs/events/{ctx.workspace_id}"
    sid = media.call_sid
    app.state.sessions[sid] = session
    early = app.state.amd_early.pop(sid, None)
    if early:
        session.notify_machine(early)
    try:
        result = await session.run()
    finally:
        app.state.sessions.pop(sid, None)
    # The audio is finished; persisting the outcome is off the call path.
    try:
        await save_platform_result(ctx, result)
    except Exception:
        log.exception("could not save result for platform call %s", ctx.call_id)


# Module-level app for `uvicorn voiceagent.server:app`.
app = create_app()


def _ensure_log_dir() -> None:
    Path("call_logs").mkdir(parents=True, exist_ok=True)


_ensure_log_dir()

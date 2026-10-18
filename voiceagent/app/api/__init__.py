"""JSON API mounted at /api."""

from fastapi import APIRouter

from . import agents, auth, calls, keys, workspaces

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(workspaces.router)
api_router.include_router(keys.router)
api_router.include_router(agents.router)
api_router.include_router(calls.router)

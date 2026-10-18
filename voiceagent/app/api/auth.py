"""Sign up, sign in, sign out, and who-am-I."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .. import repo
from ..deps import Auth, current_auth, get_db
from ..models import AuthSession, User, utcnow
from ..security import (
    CSRF_COOKIE,
    MIN_PASSWORD_LENGTH,
    SESSION_COOKIE,
    hash_password,
    login_limiter,
    new_token,
    token_id,
    verify_dummy,
    verify_password,
)
from ..settings import instance_settings

router = APIRouter(tags=["auth"])


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)
    name: str = Field("", max_length=200)
    workspace_name: str = Field("My workspace", min_length=1, max_length=200)
    # You are responsible for consent and local calling laws (TCPA, CRTC, ...). Required.
    accept_terms: bool


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


def _client_ip(request: Request) -> str:
    # Behind Caddy the real client is the first X-Forwarded-For hop.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


async def _start_session(db: AsyncSession, response: Response, user: User) -> str:
    settings = instance_settings()
    token = new_token()
    csrf = new_token()
    db.add(
        AuthSession(
            id=token_id(token),
            user_id=user.id,
            csrf_token=csrf,
            expires_at=utcnow() + timedelta(days=settings.session_days),
        )
    )
    max_age = settings.session_days * 86400
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=settings.cookie_secure, samesite="lax"
    )
    # Readable by the dashboard JS, which echoes it in X-CSRF-Token on writes.
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=max_age, httponly=False, secure=settings.cookie_secure, samesite="lax"
    )
    return csrf


def _user_out(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "is_instance_admin": user.is_instance_admin}


@router.get("/instance")
async def instance_info(db: AsyncSession = Depends(get_db)) -> dict:
    users = await repo.count_users(db)
    return {"needs_setup": users == 0, "signups_open": users == 0 or instance_settings().allow_signups}


@router.post("/auth/signup", status_code=201)
async def signup(body: SignupIn, response: Response, db: AsyncSession = Depends(get_db)) -> dict:
    first_user = await repo.count_users(db) == 0
    if not first_user and not instance_settings().allow_signups:
        raise HTTPException(403, "sign-ups are closed on this instance; ask the admin for an invite")
    if not body.accept_terms:
        raise HTTPException(422, "you must accept the terms (consent and calling-law responsibility)")
    if await repo.user_by_email(db, body.email):
        raise HTTPException(409, "an account with that email already exists")
    user = User(
        email=body.email.lower(),
        password_hash=await hash_password(body.password),
        name=body.name.strip(),
        is_instance_admin=first_user,
        tos_accepted_at=utcnow(),
    )
    db.add(user)
    await db.flush()
    ws = await repo.create_workspace(db, user, body.workspace_name)
    csrf = await _start_session(db, response, user)
    await repo.audit(db, "user.signup", user_id=user.id, workspace_id=ws.id, target=user.email)
    await db.commit()
    return {"user": _user_out(user), "workspace": {"id": ws.id, "name": ws.name, "slug": ws.slug}, "csrf_token": csrf}


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> dict:
    key = f"{_client_ip(request)}|{body.email.lower()}"
    if not login_limiter.hit(key):
        raise HTTPException(429, "too many sign-in attempts; try again in a few minutes")
    user = await repo.user_by_email(db, body.email)
    if user is None:
        await verify_dummy(body.password)
        raise HTTPException(401, "wrong email or password")
    if not user.is_active or not await verify_password(user.password_hash, body.password):
        raise HTTPException(401, "wrong email or password")
    login_limiter.reset(key)
    csrf = await _start_session(db, response, user)
    await repo.audit(db, "user.login", user_id=user.id, target=_client_ip(request))
    await db.commit()
    return {"user": _user_out(user), "csrf_token": csrf}


@router.post("/auth/logout", status_code=204)
async def logout(response: Response, auth: Auth = Depends(current_auth), db: AsyncSession = Depends(get_db)) -> Response:
    sess = await db.get(AuthSession, auth.session.id)
    if sess is not None:
        await db.delete(sess)
        await db.commit()
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    response.status_code = 204
    return response


@router.get("/auth/me")
async def me(auth: Auth = Depends(current_auth), db: AsyncSession = Depends(get_db)) -> dict:
    rows = await repo.workspaces_for(db, auth.user)
    return {
        "user": _user_out(auth.user),
        "workspaces": [{"id": ws.id, "name": ws.name, "slug": ws.slug, "role": role} for ws, role in rows],
        "csrf_token": auth.session.csrf_token,
    }

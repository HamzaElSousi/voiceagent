"""Platform API: accounts, sessions, CSRF, encrypted BYO keys, agents, call history, and tenant
isolation. Runs the real FastAPI app against a throwaway SQLite database (zero network)."""

from __future__ import annotations

from sqlalchemy import select

from voiceagent.app import repo
from voiceagent.app.crypto import SecretBox
from voiceagent.app.db import Database
from voiceagent.app.models import Call, Membership, ProviderCredential, User
from voiceagent.app.security import CSRF_COOKIE
from voiceagent.app.settings import instance_settings
from voiceagent.app.templates import agent_template

PW = "correct horse battery"


def run(client, coro_fn, *args):
    """Run an async DB helper on the app's event loop."""

    return client.portal.call(coro_fn, *args)


def csrf(client) -> dict:
    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE, "")}


def signup(client, email="owner@example.com", workspace="Acme", **extra):
    body = {"email": email, "password": PW, "name": "Owner", "workspace_name": workspace, "accept_terms": True}
    body.update(extra)
    return client.post("/api/auth/signup", json=body)


def login(client, email, password=PW):
    client.cookies.clear()
    return client.post("/api/auth/login", json={"email": email, "password": password})


def first_workspace(client) -> str:
    return client.get("/api/auth/me").json()["workspaces"][0]["id"]


async def _add_user(db: Database, email: str) -> None:
    from voiceagent.app.security import hash_password

    async with db.sessionmaker() as s:
        user = User(email=email, password_hash=await hash_password(PW))
        s.add(user)
        await s.flush()
        await repo.create_workspace(s, user, f"{email} ws")
        await s.commit()


# --------------------------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------------------------


def test_instance_needs_setup_until_first_user(platform):
    assert platform.get("/api/instance").json() == {"needs_setup": True, "signups_open": True}
    assert signup(platform).status_code == 201
    assert platform.get("/api/instance").json() == {"needs_setup": False, "signups_open": False}


def test_first_user_is_instance_admin_with_workspace(platform):
    r = signup(platform)
    body = r.json()
    assert body["user"]["is_instance_admin"] is True
    assert body["workspace"]["slug"] == "acme"
    me = platform.get("/api/auth/me").json()
    assert me["workspaces"][0]["role"] == "owner"


def test_signups_closed_after_first_user(platform):
    signup(platform)
    platform.cookies.clear()
    assert signup(platform, email="other@example.com").status_code == 403


def test_signups_open_when_allowed(platform, monkeypatch):
    signup(platform)
    monkeypatch.setenv("ALLOW_SIGNUPS", "true")
    instance_settings.cache_clear()
    platform.cookies.clear()
    r = signup(platform, email="other@example.com", workspace="Acme")
    assert r.status_code == 201
    assert r.json()["user"]["is_instance_admin"] is False
    assert r.json()["workspace"]["slug"] == "acme-2"  # slugs stay unique


def test_terms_must_be_accepted(platform):
    assert signup(platform, accept_terms=False).status_code == 422


def test_short_password_rejected(platform):
    assert signup(platform, password="short").status_code == 422


def test_password_is_hashed_not_stored(platform):
    signup(platform)

    async def _hash():
        async with platform.db.sessionmaker() as s:
            return (await s.scalar(select(User))).password_hash

    stored = run(platform, _hash)
    assert PW not in stored and stored.startswith("$argon2")


def test_login_and_wrong_password(platform):
    signup(platform)
    assert login(platform, "owner@example.com", "wrong password!!").status_code == 401
    assert login(platform, "nobody@example.com").status_code == 401
    assert login(platform, "OWNER@example.com").status_code == 200  # email is case-insensitive


def test_login_rate_limited(platform):
    signup(platform)
    codes = [login(platform, "owner@example.com", "wrong password!!").status_code for _ in range(11)]
    assert codes[-1] == 429


def test_logout_ends_session(platform):
    signup(platform)
    assert platform.post("/api/auth/logout", headers=csrf(platform)).status_code == 204
    assert platform.get("/api/auth/me").status_code == 401


def test_https_instance_sets_secure_cookies(platform, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://voice.example.com")
    instance_settings.cache_clear()
    r = signup(platform)
    cookies = r.headers.get_list("set-cookie")
    assert all("Secure" in c for c in cookies)
    assert any("va_session" in c and "HttpOnly" in c for c in cookies)


def test_unauthenticated_is_rejected(platform):
    assert platform.get("/api/workspaces").status_code == 401


def test_writes_need_csrf_token(platform):
    signup(platform)
    ws = first_workspace(platform)
    r = platform.put(f"/api/workspaces/{ws}/keys/deepgram_api_key", json={"value": "dg_secret_12345"})
    assert r.status_code == 403
    r = platform.put(
        f"/api/workspaces/{ws}/keys/deepgram_api_key", json={"value": "dg_secret_12345"}, headers=csrf(platform)
    )
    assert r.status_code == 200


# --------------------------------------------------------------------------------------------
# BYO provider keys
# --------------------------------------------------------------------------------------------


def test_keys_are_encrypted_and_never_returned(platform):
    signup(platform)
    ws = first_workspace(platform)
    secret = "sk-ant-api03-supersecretvalue-9876"
    r = platform.put(f"/api/workspaces/{ws}/keys/anthropic_api_key", json={"value": secret}, headers=csrf(platform))
    assert r.json() == {"name": "anthropic_api_key", "is_set": True, "masked": "****9876"}

    listing = platform.get(f"/api/workspaces/{ws}/keys").text
    assert secret not in listing and "****9876" in listing

    async def _cipher():
        async with platform.db.sessionmaker() as s:
            return (await s.scalar(select(ProviderCredential))).ciphertext

    cipher = run(platform, _cipher)
    assert secret not in cipher
    assert platform.box.decrypt(cipher) == secret


def test_keys_decrypt_into_call_settings(platform):
    signup(platform)
    ws = first_workspace(platform)
    platform.put(f"/api/workspaces/{ws}/keys/deepgram_api_key", json={"value": "dg_live_abcdef"}, headers=csrf(platform))

    async def _settings():
        async with platform.db.sessionmaker() as s:
            return await repo.WorkspaceRepo(s, ws).provider_settings(platform.box, "https://voice.example.com")

    settings = run(platform, _settings)
    assert settings.deepgram_api_key == "dg_live_abcdef"
    assert settings.public_base_url == "https://voice.example.com"
    assert "DEEPGRAM_API_KEY" not in settings.missing_for_live_call()


def test_unknown_key_rejected(platform):
    signup(platform)
    ws = first_workspace(platform)
    r = platform.put(f"/api/workspaces/{ws}/keys/aws_secret", json={"value": "x"}, headers=csrf(platform))
    assert r.status_code == 404


def test_delete_key(platform):
    signup(platform)
    ws = first_workspace(platform)
    platform.put(f"/api/workspaces/{ws}/keys/calcom_api_key", json={"value": "cal_live_1234"}, headers=csrf(platform))
    assert platform.delete(f"/api/workspaces/{ws}/keys/calcom_api_key", headers=csrf(platform)).status_code == 204
    keys = {k["name"]: k for k in platform.get(f"/api/workspaces/{ws}/keys").json()}
    assert keys["calcom_api_key"]["is_set"] is False


# --------------------------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------------------------


def test_agent_crud_with_template(platform):
    signup(platform)
    ws = first_workspace(platform)
    template = platform.get("/api/agent-template?mode=inbound").json()
    assert template["mode"] == "inbound"
    r = platform.post(f"/api/workspaces/{ws}/agents", json={"name": "Front desk", "config": template}, headers=csrf(platform))
    assert r.status_code == 201
    agent = r.json()
    assert agent["mode"] == "inbound"

    cfg = dict(agent["config"], mode="outbound")
    r = platform.patch(f"/api/workspaces/{ws}/agents/{agent['id']}", json={"config": cfg}, headers=csrf(platform))
    assert r.json()["mode"] == "outbound"
    assert len(platform.get(f"/api/workspaces/{ws}/agents").json()) == 1
    assert platform.delete(f"/api/workspaces/{ws}/agents/{agent['id']}", headers=csrf(platform)).status_code == 204
    assert platform.get(f"/api/workspaces/{ws}/agents").json() == []


def test_invalid_agent_config_is_422(platform):
    signup(platform)
    ws = first_workspace(platform)
    bad = dict(agent_template("outbound"), endpointing_ms=5)
    r = platform.post(f"/api/workspaces/{ws}/agents", json={"name": "x", "config": bad}, headers=csrf(platform))
    assert r.status_code == 422


def test_secrets_in_agent_config_rejected(platform):
    signup(platform)
    ws = first_workspace(platform)
    cfg = agent_template("outbound")
    cfg["llm"]["api_key"] = "sk-ant-oops"
    r = platform.post(f"/api/workspaces/{ws}/agents", json={"name": "x", "config": cfg}, headers=csrf(platform))
    assert r.status_code == 422
    assert "Secrets must live" in r.text


def test_inbound_webhook_url_uses_slug(platform, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://voice.example.com")
    monkeypatch.setenv("COOKIE_SECURE", "false")  # the test client speaks plain http
    instance_settings.cache_clear()
    signup(platform)
    ws = platform.get("/api/workspaces").json()[0]
    assert ws["inbound_webhook_url"] == "https://voice.example.com/voice/inbound/acme"


# --------------------------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------------------------


def _two_tenants(platform):
    signup(platform)
    ws_a = first_workspace(platform)
    platform.put(f"/api/workspaces/{ws_a}/keys/anthropic_api_key", json={"value": "sk-a-secret-1111"}, headers=csrf(platform))
    agent_a = platform.post(
        f"/api/workspaces/{ws_a}/agents", json={"name": "A agent", "config": agent_template("outbound")}, headers=csrf(platform)
    ).json()

    async def _call():
        async with platform.db.sessionmaker() as s:
            s.add(Call(workspace_id=ws_a, contact_name="Secret Lead", contact_phone="+15555550100"))
            await s.commit()

    run(platform, _call)
    run(platform, _add_user, platform.db, "b@example.com")
    assert login(platform, "b@example.com").status_code == 200
    return ws_a, agent_a


def test_other_tenant_cannot_see_workspace_or_data(platform):
    ws_a, agent_a = _two_tenants(platform)
    assert platform.get(f"/api/workspaces/{ws_a}").status_code == 404
    assert platform.get(f"/api/workspaces/{ws_a}/keys").status_code == 404
    assert platform.get(f"/api/workspaces/{ws_a}/agents").status_code == 404
    assert platform.get(f"/api/workspaces/{ws_a}/calls").status_code == 404
    assert platform.get(f"/api/workspaces/{ws_a}/agents/{agent_a['id']}").status_code == 404
    assert [w["id"] for w in platform.get("/api/workspaces").json()] != [ws_a]


def test_other_tenant_cannot_modify(platform):
    ws_a, agent_a = _two_tenants(platform)
    h = csrf(platform)
    assert platform.put(f"/api/workspaces/{ws_a}/keys/anthropic_api_key", json={"value": "x"}, headers=h).status_code == 404
    assert platform.delete(f"/api/workspaces/{ws_a}/agents/{agent_a['id']}", headers=h).status_code == 404


def test_agent_ids_are_scoped_to_workspace(platform):
    # B uses their OWN workspace id with A's agent id: still not found.
    _ws_a, agent_a = _two_tenants(platform)
    ws_b = first_workspace(platform)
    assert platform.get(f"/api/workspaces/{ws_b}/agents/{agent_a['id']}").status_code == 404
    assert platform.delete(f"/api/workspaces/{ws_b}/agents/{agent_a['id']}", headers=csrf(platform)).status_code == 404
    assert platform.get(f"/api/workspaces/{ws_b}/calls").json() == []


def test_member_role_is_read_only(platform):
    ws_a, _agent = _two_tenants(platform)

    async def _join():
        async with platform.db.sessionmaker() as s:
            b = await repo.user_by_email(s, "b@example.com")
            s.add(Membership(workspace_id=ws_a, user_id=b.id, role="member"))
            await s.commit()

    run(platform, _join)
    assert platform.get(f"/api/workspaces/{ws_a}/agents").status_code == 200
    calls = platform.get(f"/api/workspaces/{ws_a}/calls").json()
    assert calls[0]["contact_name"] == "Secret Lead"
    r = platform.put(f"/api/workspaces/{ws_a}/keys/anthropic_api_key", json={"value": "x"}, headers=csrf(platform))
    assert r.status_code == 403


# --------------------------------------------------------------------------------------------
# Instance CLI
# --------------------------------------------------------------------------------------------


def test_init_instance_generates_and_never_overwrites(tmp_path):
    from typer.testing import CliRunner

    from voiceagent.cli import app

    env = tmp_path / ".env"
    env.write_text("SECRET_KEY=\nDEEPGRAM_API_KEY=keepme\n")
    runner = CliRunner()
    r = runner.invoke(app, ["init-instance", "--env-file", str(env), "--domain", "voice.example.com"])
    assert r.exit_code == 0, r.output
    text = env.read_text()
    assert "DEEPGRAM_API_KEY=keepme" in text
    assert "PUBLIC_BASE_URL=https://voice.example.com" in text
    first_key = [ln for ln in text.splitlines() if ln.startswith("ENCRYPTION_KEY=")][0]
    SecretBox(first_key.split("=", 1)[1])  # a valid Fernet key
    runner.invoke(app, ["init-instance", "--env-file", str(env)])
    assert first_key in env.read_text()  # second run must not rotate the key


def test_migration_matches_models(tmp_path):
    """Alembic head builds the same tables as the models (catches a forgotten migration)."""

    import asyncio

    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    from voiceagent.app.db import Base
    from voiceagent.app.migrate import upgrade

    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    upgrade(url)

    async def _tables():
        engine = create_async_engine(url)
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        await engine.dispose()
        return names

    tables = asyncio.run(_tables()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)

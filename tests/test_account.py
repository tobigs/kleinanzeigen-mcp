"""Browser login stores a token. It never submits a password."""

import base64
import json
import os
import stat

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from kleinanzeigen import account

pytestmark = pytest.mark.anyio


def _jwt(email: str) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return ".".join([part({"alg": "none"}), part({"email": email}), "sig"])


@pytest.fixture
def token_file(monkeypatch, tmp_path):
    path = tmp_path / "token.json"
    monkeypatch.setenv("KLEINANZEIGEN_TOKEN_FILE", str(path))
    for name in ("KLEINANZEIGEN_ACCESS_TOKEN", "KLEINANZEIGEN_USER_EMAIL", "KLEINANZEIGEN_USER_ID",
                 "KLEINANZEIGEN_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return path


def test_the_login_url_is_the_real_authorize_endpoint():
    url = account.build_login("verifier", "challenge", "state")
    assert url.startswith("https://login.kleinanzeigen.de/authorize?")
    assert "client_id=uV5j90myVPc2XzEOFuWUD2At17OACEGQ" in url
    assert "code_challenge=challenge" in url and "code_challenge_method=S256" in url
    assert "password" not in url


async def test_complete_login_exchanges_the_code_and_stores_the_account(ka, token_file):
    ka.on("login.kleinanzeigen.de", "/oauth/token", {
        "access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600,
        "id_token": _jwt("a@b.de"),
    })
    ka.on("api.kleinanzeigen.de", "/api/users/", {"id": 42, "contactName": "Ada"})
    signed_in = await account.complete_login(
        "https://login.kleinanzeigen.de/android/com.ebay.kleinanzeigen/callback?code=abc&state=st",
        "verifier", "st")
    assert (signed_in.user_id, signed_in.email, signed_in.token) == ("42", "a@b.de", "access-1")
    saved = json.loads(token_file.read_text())
    assert saved["refresh_token"] == "refresh-1" and saved["user_id"] == "42"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    profile = ka.sent("/api/users/")[0]
    assert profile.headers["x-ecg-authorization-user"] == "email=a@b.de,access=access-1"
    assert "code" not in token_file.read_text()


async def test_a_state_mismatch_does_not_call_the_token_endpoint(ka, token_file):
    with pytest.raises(ToolError, match="state"):
        await account.complete_login("https://login.kleinanzeigen.de/callback?code=abc&state=other", "v", "st")
    assert ka.requests == []


async def test_an_expired_refresh_token_is_exchanged_once(ka, token_file):
    token_file.write_text(json.dumps({
        "access_token": "old", "refresh_token": "refresh-1", "expires_at": 0,
        "email": "a@b.de", "user_id": "42",
    }))
    os.chmod(token_file, 0o600)
    ka.on("login.kleinanzeigen.de", "/oauth/token", {
        "access_token": "new", "refresh_token": "refresh-2", "expires_in": 120,
    })
    signed_in = await account.require_account()
    assert signed_in.token == "new"
    assert json.loads(token_file.read_text())["refresh_token"] == "refresh-2"
    assert len(ka.sent("/oauth/token")) == 1

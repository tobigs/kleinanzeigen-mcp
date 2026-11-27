"""The signed-in account seller tools act as.

The app API's user routes accept a token in ``X-ECG-Authorization-User``
(``email=<email>,access=<token>``). That header was confirmed live: without it
the server says authentication is mandatory, and with a token that does not
belong to the id in the URL it says the token does not match that user. A
password is also accepted by the challenge header and is intentionally not
implemented here, so this client never tries to bypass a login page or a
CAPTCHA. The person signs in themselves (``python -m kleinanzeigen.login``)
and the resulting token is read from the environment or from a mode-0600 file.
"""

import base64
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from . import api


TOKEN_FILE_ENV = "KLEINANZEIGEN_TOKEN_FILE"
DEFAULT_TOKEN_PATH = Path.home() / ".kleinanzeigen-mcp" / "token.json"

AUTH_HELP = (
    "This call needs the account that owns the ads. Set KLEINANZEIGEN_ACCESS_TOKEN "
    "and KLEINANZEIGEN_USER_EMAIL (and KLEINANZEIGEN_USER_ID, or let the login command "
    "look it up), or run `python -m kleinanzeigen.login` and sign in in your browser. "
    "Password login and CAPTCHA are not automated."
)


@dataclass(frozen=True)
class Account:
    user_id: str
    email: str
    token: str


def token_path() -> Path:
    override = os.environ.get(TOKEN_FILE_ENV, "").strip()
    return Path(override).expanduser() if override else DEFAULT_TOKEN_PATH


def _reject_header_value(label: str, value: str) -> None:
    """The user header is an unquoted ``email=...,access=...`` pair. A comma
    or whitespace in either half would be read as a second field."""
    if not value or any(ch in value for ch in ", \t\r\n"):
        raise ToolError(f"{label} cannot be empty or contain a comma or whitespace.")


def _account(user_id: str, email: str, token: str) -> Account:
    _reject_header_value("email", email)
    _reject_header_value("access token", token)
    if not str(user_id).isdigit():
        raise ToolError(f"KLEINANZEIGEN_USER_ID must be the numeric account id (got {user_id!r}).")
    return Account(str(user_id), email, token)


def _from_env() -> Optional[Account]:
    token = os.environ.get("KLEINANZEIGEN_ACCESS_TOKEN", "").strip()
    email = os.environ.get("KLEINANZEIGEN_USER_EMAIL", "").strip()
    user_id = os.environ.get("KLEINANZEIGEN_USER_ID", "").strip()
    if not token and not email and not user_id:
        return None
    if not token or not email:
        raise ToolError(
            "Set both KLEINANZEIGEN_ACCESS_TOKEN and KLEINANZEIGEN_USER_EMAIL. " + AUTH_HELP
        )
    if not user_id:
        return None  # token and email are present; the caller resolves the id
    return _account(user_id, email, token)


def _env_identity() -> Optional[tuple]:
    """Token and email from the environment when the numeric id is still unknown."""
    token = os.environ.get("KLEINANZEIGEN_ACCESS_TOKEN", "").strip()
    email = os.environ.get("KLEINANZEIGEN_USER_EMAIL", "").strip()
    user_id = os.environ.get("KLEINANZEIGEN_USER_ID", "").strip()
    if token and email and not user_id:
        _reject_header_value("email", email)
        _reject_header_value("access token", token)
        return email, token
    return None


def _read_store() -> dict:
    path = token_path()
    try:
        if not path.is_file():
            return {}
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o600)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ToolError(f"Could not read the saved login at {path} ({type(exc).__name__}).") from exc
    return data if isinstance(data, dict) else {}


def write_store(data: dict) -> None:
    """Replace the token file. The refresh token is a password, so the file
    is owner-readable only."""
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _jwt_claims(token: str) -> dict:
    """Read an Auth0 id token's payload. Kleinanzeigen checks the signature;
    we only need the email claim to build the user header."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (IndexError, json.JSONDecodeError, ValueError, UnicodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


async def lookup_user_id(email: str, token: str) -> str:
    """GET ``/users/{email}/profile.json``. Live, this route answers 401 until
    the user header matches that account, then returns the numeric id."""
    # The id is not part of this URL; the email is. "0" only fills the header object.
    provisional = Account("0", email, token)
    try:
        data = await api.get(f"users/{email}/profile.json", account=provisional)
    except httpx.HTTPStatusError as exc:
        raise ToolError(api.upstream_message(exc.response.status_code, "account", exc.response.text)) from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"could not reach Kleinanzeigen for the account request ({type(exc).__name__})") from exc
    user_id = data.get("id") if isinstance(data, dict) else None
    if user_id is None and isinstance(data, dict):
        user_id = (data.get("data") or {}).get("id")
    if not str(user_id or "").isdigit():
        raise ToolError(
            "The account profile did not include a numeric id. Set KLEINANZEIGEN_USER_ID."
        )
    return str(user_id)


async def require_account() -> Account:
    """The account for this process: environment first, then the token file."""
    env = _from_env()
    if env is not None:
        return env
    identity = _env_identity()
    if identity is not None:
        email, token = identity
        return _account(await lookup_user_id(email, token), email, token)

    stored = _read_store()
    refresh = os.environ.get("KLEINANZEIGEN_REFRESH_TOKEN", "").strip() or stored.get("refresh_token")
    access = stored.get("access_token") or ""
    expires = stored.get("expires_at") or 0
    if refresh and (not access or time.time() >= float(expires) - 60):
        stored = await refresh_login(str(refresh), stored)
        access = stored.get("access_token") or ""
    email = stored.get("email") or ""
    user_id = str(stored.get("user_id") or "")
    if access and email and user_id.isdigit():
        return _account(user_id, email, access)
    if access and email:
        return _account(await lookup_user_id(email, access), email, access)
    raise ToolError(AUTH_HELP)


async def refresh_login(refresh_token: str, previous: Optional[dict] = None) -> dict:
    """Exchange a refresh token at Auth0. One attempt: the refresh token rotates
    and a retry could race that rotation."""
    payload = await _token_request({
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
    })
    return _store_tokens(payload, previous or {})


# Auth0 settings the Android app publishes. The authorize URL was requested
# live and redirected to the real login page; nothing here submits a password.
CLIENT_ID = "uV5j90myVPc2XzEOFuWUD2At17OACEGQ"
REDIRECT_URI = "https://login.kleinanzeigen.de/android/com.ebay.kleinanzeigen/callback"
AUTHORIZE_URL = "https://login.kleinanzeigen.de/authorize"
TOKEN_URL = "https://login.kleinanzeigen.de/oauth/token"
SCOPE = "openid email profile offline_access"


def build_login(verifier: str, challenge: str, state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "prompt": "login",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def complete_login(redirect_url: str, verifier: str, expected_state: str) -> Account:
    """Turn the browser's final URL into stored tokens. The person has already
    passed whatever login page or CAPTCHA Auth0 showed them."""
    query = parse_qs(urlparse(redirect_url.strip()).query)
    if "error" in query:
        raise ToolError(
            f"Login was refused: {query.get('error', [''])[0]} "
            f"{query.get('error_description', [''])[0]}".strip()
        )
    code = (query.get("code") or [None])[0]
    if not code:
        raise ToolError("That URL has no ?code=. Paste the full address the browser ended on.")
    if (query.get("state") or [None])[0] != expected_state:
        raise ToolError("Login state did not match. Start again with python -m kleinanzeigen.login.")
    payload = await _token_request({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    })
    stored = _store_tokens(payload, {})
    email = stored.get("email") or ""
    token = stored.get("access_token") or ""
    if not email:
        raise ToolError("The login response had no email. Set KLEINANZEIGEN_USER_EMAIL and the token.")
    user_id = await lookup_user_id(email, token)
    stored["user_id"] = user_id
    write_store(stored)
    return _account(user_id, email, token)


def _store_tokens(payload: dict, previous: dict) -> dict:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise ToolError("The token endpoint did not return an access token.")
    stored = dict(previous)
    stored["access_token"] = access
    stored["expires_at"] = time.time() + int(payload.get("expires_in") or 600)
    if payload.get("refresh_token"):
        stored["refresh_token"] = payload["refresh_token"]
    email = _jwt_claims(payload.get("id_token") or "").get("email")
    if isinstance(email, str) and email:
        stored["email"] = email
    # drop a half-finished login so the next call does not reuse its verifier
    stored.pop("pending", None)
    write_store(stored)
    return stored


async def _token_request(payload: dict) -> dict:
    """POST the Auth0 token endpoint with plain httpx. No app Basic header and
    no retry: an authorization code works once."""
    await api._pace()
    response = await api.client.post(TOKEN_URL, json=payload, headers={
        "accept": "application/json",
        "user-agent": api.KA_CLIENT,
    })
    if response.status_code != 200:
        raise ToolError(
            f"The login token endpoint returned {response.status_code}. "
            "Sign in again with python -m kleinanzeigen.login."
        )
    data = response.json()
    if not isinstance(data, dict):
        raise ToolError("The login token endpoint did not return a JSON object.")
    return data

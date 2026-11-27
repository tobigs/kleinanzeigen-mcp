"""Requests to Kleinanzeigen's app API: the client credential, retries, the
error messages the model sees, and unwrapping its JAXB-shaped JSON."""

import asyncio
import functools
import json
import logging
import re
import time
import uuid
from typing import Literal, Optional

import httpx
from mcp.server.mcpserver.exceptions import ToolError


logger = logging.getLogger(__name__)


BASE_URL = "https://api.kleinanzeigen.de/api"
GATEWAY_URL = "https://gateway.kleinanzeigen.de"

KA_CLIENT = "Kleinanzeigen/2026.37.1 (Android 14; google sdk_gphone64_x86_64)"
KA_ECG_USER_AGENT = "ebayk-android-app-2026.37.1"
KA_ECG_USER_VERSION = "2026.37.1"

# --- Request authentication ------------------------------------------------
# Every request carries the static HTTP Basic credential the Android app sends,
# the same for every user.
KA_BASIC_AUTH = "Basic YW5kcm9pZDpUYVI2MHBFdHRZ"

# `x-ebayk-app` identifies the app installation: a UUIDv4 immediately followed
# by the epoch-millis timestamp of when it was generated, no separator (e.g.
# "7243bedb-2427-4f72-bf89-567ecb1c74661789285561410"). A fresh,
# never-before-seen value is accepted immediately with no separate
# registration call, so it looks like a free-form client-generated id the
# server does not validate -- generated once per process and reused here.
_installation_id = f"{uuid.uuid4()}{int(time.time() * 1000)}"


MAX_RETRIES = 2
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Space every call, including retries. Seller tools share this with search so a
# model paging through ads cannot burst the app API. Tests replace asyncio.sleep.
MIN_INTERVAL = 0.4
_pace_lock = asyncio.Lock()
_last_request_at = 0.0


client = httpx.AsyncClient(http2=True)


# Confirmed against the server's own ads-search-options schema
# (api/ads/search-metadata/{categoryId}.json) rather than guessed.
Sort = Literal["RECOMMENDED", "DATE_DESCENDING", "PRICE_ASCENDING", "PRICE_DESCENDING", "DISTANCE_ASCENDING"]
AdType = Literal["OFFERED", "WANTED"]
PosterType = Literal["PRIVATE", "COMMERCIAL"]


def _server_text(body: str) -> str:
    """The short message inside an api-base-error or a gateway errors array."""
    messages = re.findall(r"<message>(.*?)</message>", body or "", flags=re.DOTALL)
    if not messages:
        try:
            data = json.loads(body or "")
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict):
            messages = [item.get("message", "") for item in data.get("errors") or []
                        if isinstance(item, dict)]
    text = "; ".join(part.strip() for part in messages if str(part).strip())
    return text[:300]


def upstream_message(status: int, what: str, body: str = "") -> str:
    """Phrase an HTTP failure in terms the caller can act on, without the
    internal url and MDN link httpx puts in its own message."""
    detail = _server_text(body)
    if status == 404:
        return f"no {what} found -- wrong id, or the ad has been taken down"
    if status == 400:
        suffix = f": {detail}" if detail else "; check the arguments you passed"
        return f"Kleinanzeigen rejected the {what} request (400){suffix}"
    if status in (401, 403):
        # a blocked IP range and a stale credential both come back as 403, but
        # only the block says so in the body -- they need opposite fixes
        if "gesperrt" in body or "blocked" in body.lower():
            return (f"Kleinanzeigen blocked this IP range on the {what} request ({status}). Their "
                    f"anti-fraud rejects whole ranges (VPN and datacenter IPs in particular), so "
                    f"this usually needs a different connection, not a retry. Search and ad "
                    f"lookups are unaffected -- only the seller profile enforces it.")
        # Account routes name the user header. That is a missing or wrong login,
        # not the static app credential every anonymous call uses.
        if ("X-ECG-Authorization-User" in body or "mandatory for /users" in body
                or "incorrect password" in body or "doesn't match" in body):
            return (f"Kleinanzeigen rejected the account login on the {what} request ({status}). "
                    f"Set KLEINANZEIGEN_ACCESS_TOKEN and KLEINANZEIGEN_USER_EMAIL for the account "
                    f"that owns this user id, or run python -m kleinanzeigen.login. Password login "
                    f"and CAPTCHA are not automated.")
        return (f"Kleinanzeigen refused the {what} request ({status}) -- the client credential "
                f"in kleinanzeigen/api.py (KA_BASIC_AUTH) may be outdated")
    if status == 429:
        return f"Kleinanzeigen rate-limited the {what} request (429) -- slow down and retry later"
    suffix = f": {detail}" if detail else ""
    return f"Kleinanzeigen failed on the {what} request (HTTP {status}){suffix}"


def tool_errors(what: str):
    """Turn upstream failures into a ``ToolError`` the model can read, rather
    than a raw httpx error; any exception that is not a ``ToolError`` would only
    reach it as "Error executing tool". ``what`` names the thing being fetched,
    for the message."""
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except httpx.HTTPStatusError as exc:
                raise ToolError(upstream_message(exc.response.status_code, what, exc.response.text)) from exc
            except httpx.HTTPError as exc:
                raise ToolError(f"could not reach Kleinanzeigen for the {what} request "
                                f"({type(exc).__name__})") from exc
        return wrapper
    return decorator


def _auth_headers(*, user_token: str = "", account=None, gateway: bool = False) -> dict:
    """Headers the app sends on api.kleinanzeigen.de / gateway.kleinanzeigen.de.

    Anonymous calls keep the static Basic credential. An account call on the
    API host adds ``X-ECG-Authorization-User`` (``email=...,access=...``): a
    live 401 showed that ``x-ebayk-userid-token`` alone is ignored, while that
    header is the one the server's ``X-ECG-Authenticate-User`` challenge names.
    The message gateway is a different host; it is sent the same user header
    plus ``Authorization: Bearer``. Which of those two the gateway checks could
    not be told apart, because both an absent token and a wrong one returned an
    empty 401.
    """
    token = account.token if account is not None else user_token
    headers = {
        "x-ebayk-app": _installation_id,
        "x-ebayk-userid-token": token,
        "user-agent": KA_CLIENT,
        "x-ecg-user-agent": KA_ECG_USER_AGENT,
        "x-ecg-user-version": KA_ECG_USER_VERSION,
        "accept": "application/json",
        "accept-encoding": "gzip",
    }
    if account is not None and gateway:
        headers["authorization"] = f"Bearer {account.token}"
        headers["x-ecg-authorization-user"] = f"email={account.email},access={account.token}"
    else:
        headers["authorization"] = KA_BASIC_AUTH
        if account is not None:
            headers["x-ecg-authorization-user"] = f"email={account.email},access={account.token}"
    return headers


async def _pace() -> None:
    """Wait out the gap since the previous call. The lock is held across the
    sleep so concurrent tool calls share one interval instead of bursting."""
    global _last_request_at
    async with _pace_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()


def _idempotent(method: str) -> bool:
    """POST is not retried. A retry can publish a second ad, send a second
    message, or store a second picture after the first request already landed."""
    return method in {"GET", "HEAD", "PUT", "DELETE"}


async def send(method: str, url: str, *, params=None, json_body=None, content=None,
               files=None, content_type: Optional[str] = None, account=None) -> httpx.Response:
    """Send one call and return the response. Idempotent methods retry timeouts
    and 429/5xx. POST does not."""
    method = method.upper()
    headers = _auth_headers(account=account, gateway=url.startswith(GATEWAY_URL))
    if content_type:
        headers["content-type"] = content_type
    retryable = _idempotent(method)
    for attempt in range(MAX_RETRIES + 1):
        await _pace()
        try:
            response = await client.request(
                method, url, params=params, json=json_body, content=content, files=files,
                headers=headers,
            )
        except httpx.TransportError:
            if not retryable or attempt == MAX_RETRIES:
                raise
        else:
            if (response.status_code not in RETRYABLE_STATUS or not retryable
                    or attempt == MAX_RETRIES):
                response.raise_for_status()
                return response
        delay = 0.5 * (2 ** attempt)
        logger.warning("Request to '%s' failed (attempt %d/%d), retrying in %.1fs",
                       url, attempt + 1, MAX_RETRIES + 1, delay)
        await asyncio.sleep(delay)
    raise RuntimeError(f"{method} {url} failed")  # unreachable; keeps the type checker quiet


async def get(endpoint: str, params: Optional[dict] = None, *, account=None) -> dict:
    """GET an api.kleinanzeigen.de endpoint. See `send`."""
    response = await send("GET", f"{BASE_URL}/{endpoint}", params=params, account=account)
    return response.json()


async def get_gateway(endpoint: str, params: Optional[dict] = None, *, account=None) -> dict:
    """GET a gateway.kleinanzeigen.de endpoint (newer services: seller
    reputation, homepage feed, messages, ...). See `send`."""
    response = await send("GET", f"{GATEWAY_URL}/{endpoint}", params=params, account=account)
    return response.json()


# Kleinanzeigen's JSON is JAXB-serialized XML (com.ebay.ecg.api.spec...), so
# scalar fields don't come as plain values -- they're wrapped as {"value": ...},
# and top-level collections are keyed by their XML namespace URI. `unwrap` unwraps
# the former; `collection` picks out the latter regardless of the exact namespace string.
def unwrap(node, default=None):
    return node.get("value", default) if isinstance(node, dict) else default


def collection(container: dict, local_name: str) -> dict:
    for key, val in container.items():
        if key.endswith(f"}}{local_name}") or key == local_name:
            return unwrap(val, {})
    return {}

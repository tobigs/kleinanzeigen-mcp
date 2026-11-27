"""Seller tools through an MCP client. The account is an env token, never a password."""

import httpx
import pytest

from conftest import ToolFailed
from samples import ad, detail, picture, posting_metadata, search

pytestmark = pytest.mark.anyio

API = "api.kleinanzeigen.de"
GATEWAY = "gateway.kleinanzeigen.de"
PC = posting_metadata(attributes=[
    ("pc_zubehoer_software.art", "required", [("grafikkarten", "Grafikkarten")]),
    ("pc_zubehoer_software.versand", "required", [("ja", "Ja")]),
])
ATTRS = {"pc_zubehoer_software.art": "grafikkarten", "pc_zubehoer_software.versand": "ja"}


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setenv("KLEINANZEIGEN_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("KLEINANZEIGEN_USER_EMAIL", "a@b.de")
    monkeypatch.setenv("KLEINANZEIGEN_USER_ID", "42")
    monkeypatch.setenv("KLEINANZEIGEN_TOKEN_FILE", "/tmp/kleinanzeigen-mcp-test-token-absent.json")


@pytest.fixture
def signed_out(monkeypatch):
    for name in ("KLEINANZEIGEN_ACCESS_TOKEN", "KLEINANZEIGEN_USER_EMAIL", "KLEINANZEIGEN_USER_ID",
                 "KLEINANZEIGEN_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KLEINANZEIGEN_TOKEN_FILE", "/tmp/kleinanzeigen-mcp-test-token-absent.json")


def _listing(**kwargs):
    base = dict(title="Grafikkarte", description="Funktionstüchtig", category=225, location_id=3331,
                price=12.5, price_type="VB", attributes=ATTRS)
    base.update(kwargs)
    return base


async def test_listing_tools_refuse_without_an_account(ka, call, signed_out):
    with pytest.raises(ToolFailed, match="KLEINANZEIGEN_ACCESS_TOKEN"):
        await call("get_my_listings")
    with pytest.raises(ToolFailed, match="KLEINANZEIGEN_ACCESS_TOKEN"):
        await call("delete_listing", ad_id=7, confirm=True)
    assert ka.requests == []


async def test_public_search_does_not_send_the_account_token(ka, call, signed_in):
    ka.on(API, "/api/ads.json", search([]))
    await call("search_kleinanzeigen", query="x")
    headers = ka.sent("/api/ads.json")[0].headers
    assert "x-ecg-authorization-user" not in headers
    assert headers["authorization"].startswith("Basic ")


async def test_my_listings_use_the_account_header(ka, call, signed_in):
    ka.on(API, "/api/users/42/ads.json", search([ad(7, "Bike", description="rot")]))
    result = await call("get_my_listings", rows=10)
    assert result["results"][0]["title"] == "Bike"
    assert result["results"][0]["ad_status"] == "ACTIVE"
    sent = ka.sent("/api/users/42/ads.json")[0]
    assert sent.headers["x-ecg-authorization-user"] == "email=a@b.de,access=tok"
    assert sent.url.params["page"] == "0" and sent.url.params["size"] == "10"


async def test_a_rejected_account_token_is_not_blamed_on_the_app_credential(ka, call, signed_in):
    ka.on(API, "/api/users/42/ads.json", httpx.Response(
        401, text='<?xml version="1.0"?><api-base-error><api-errors><api-error>'
                  "<message>Authentication is mandatory for /users resource. "
                  "check X-ECG-Authorization-User</message></api-error></api-errors></api-base-error>"))
    with pytest.raises(ToolFailed, match="account login"):
        await call("get_my_listings")


async def test_get_retries_and_create_does_not(ka, call, signed_in):
    state = {"n": 0}

    def flaky(_request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, text="busy")
        return search([ad(7, "Bike")])

    ka.on(API, "/api/users/42/ads.json", flaky)
    assert (await call("get_my_listings"))["results"][0]["id"] == "7"
    assert state["n"] == 2

    posts = {"n": 0}

    def once(_request):
        posts["n"] += 1
        return httpx.Response(500, text="<message>nope</message>")

    ka.on(API, "/api/ads/metadata/225.json", PC)
    ka.on(API, "/api/users/42/ads.json", once)
    with pytest.raises(ToolFailed, match="nope"):
        await call("create_listing", confirm=True, **_listing())
    assert posts["n"] == 1


async def test_create_previews_then_posts_the_schema_body(ka, call, signed_in):
    ka.on(API, "/api/ads/metadata/225.json", PC)
    preview = await call("create_listing", **_listing())
    assert preview["status"] == "confirmation_required"
    assert preview["price_type"] == "PLEASE_CONTACT"
    assert all(request.method == "GET" for request in ka.requests)

    ka.on(API, "/api/users/42/ads.json", httpx.Response(
        201, headers={"Location": "https://api.kleinanzeigen.de/api/users/42/ads/99.json"}))
    created = await call("create_listing", confirm=True, **_listing(title="A & B"))
    assert created["id"] == "99"
    sent = [request for request in ka.requests if request.method == "POST"][-1]
    body = sent.content.decode()
    assert "PLEASE_CONTACT" in body and "grafikkarten" in body and "a@b.de" in body
    assert "A &amp; B" in body and "latitude" not in body
    assert sent.headers["content-type"].startswith("application/xml")


async def test_create_rejects_a_bad_attribute_before_posting(ka, call, signed_in):
    ka.on(API, "/api/ads/metadata/225.json", PC)
    with pytest.raises(ToolFailed, match="pc_zubehoer_software.versand"):
        await call("create_listing", confirm=True, **_listing(attributes={"pc_zubehoer_software.art": "grafikkarten"}))
    assert [request.method for request in ka.requests] == ["GET"]


async def test_update_pause_activate_and_delete_need_confirm(ka, call, signed_in):
    ka.on(API, "/api/users/42/ads/paused/", httpx.Response(204))
    ka.on(API, "/api/users/42/ads/active/", httpx.Response(204))
    ka.on(API, "/api/users/42/ads/7", httpx.Response(204))
    ka.on(API, "/api/users/42/ads/7.json", detail(7, "Bike"))
    preview = await call("update_listing", ad_id=7, title="Neu")
    assert preview["status"] == "confirmation_required"
    assert preview["keeps_omitted_fields"] == "not verified live"
    assert ka.requests == []
    updated = await call("update_listing", ad_id=7, title="Neu", confirm=True)
    assert updated["fields"] == ["title"]
    put = ka.sent("/api/users/42/ads/7.json")[-1]
    assert put.method == "PUT" and "<ad:title>Neu</ad:title>" in put.content.decode()
    assert "price" not in put.content.decode()

    paused = await call("pause_listing", ad_id=7)
    assert paused["status"] == "confirmation_required" and paused["title"] == "Bike"
    assert not any(request.method == "PUT" and "paused" in request.url.path for request in ka.requests)
    await call("pause_listing", ad_id=7, confirm=True)
    assert any(request.method == "PUT" and request.url.path.endswith("/ads/paused/7.json") for request in ka.requests)

    await call("activate_listing", ad_id=7, confirm=True)
    assert any(request.method == "PUT" and request.url.path.endswith("/ads/active/7.json") for request in ka.requests)

    deleted = await call("delete_listing", ad_id=7)
    assert deleted["permanent"] is True
    assert not any(request.method == "DELETE" for request in ka.requests)
    await call("delete_listing", ad_id=7, confirm=True)
    assert any(request.method == "DELETE" and request.url.path.endswith("/ads/7") for request in ka.requests)


async def test_upload_checks_the_file_then_posts_it(ka, call, tmp_path, monkeypatch):
    image = tmp_path / "dot.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"jpeg-bytes")
    monkeypatch.setenv("KA_UPLOAD_ROOTS", str(tmp_path))
    preview = await call("upload_images", paths=[str(image)])
    assert preview["status"] == "confirmation_required" and ka.requests == []
    ka.on(API, "/api/pictures.json", picture("https://img.kleinanzeigen.de/api/v1/prod-ads/images/ab/abc"))
    uploaded = await call("upload_images", paths=[str(image)], confirm=True)
    assert uploaded["images"][0]["url"].endswith("/abc")
    sent = ka.sent("/api/pictures.json")[0]
    assert sent.headers["content-type"].startswith("multipart/form-data")
    assert b"\xff\xd8\xff" in sent.content
    other = tmp_path / "note.txt"
    other.write_bytes(b"hello")
    with pytest.raises(ToolFailed, match="JPEG"):
        await call("upload_images", paths=[str(other)], confirm=True)
    outside = tmp_path.parent / "outside.jpg"
    outside.write_bytes(b"\xff\xd8\xff")
    with pytest.raises(ToolFailed, match="KA_UPLOAD_ROOTS"):
        await call("upload_images", paths=[str(outside)], confirm=True)


async def test_messages_read_with_get_and_send_only_when_confirmed(ka, call, signed_in):
    ka.on(GATEWAY, "/messagebox/api/users/42/conversations", {
        "conversations": [{
            "id": "c1", "adId": "7", "adTitle": "Bike", "role": "SELLER", "buyerName": "Ada",
            "unread": True, "unreadMessagesCount": 2, "textShortTrimmed": "Noch da?",
            "receivedDate": "2026-11-01T10:00:00",
        }],
    })
    inbox = await call("get_messages")
    assert inbox["conversations"][0]["counterparty"] == "Ada"
    assert ka.sent("/messagebox/")[0].method == "GET"

    ka.on(GATEWAY, "/messagebox/api/users/42/conversations/c1", {
        "messages": [{"text": "Noch da?", "boundness": "INBOUND", "receivedDate": "2026-11-01T10:00:00"}],
    })
    thread = await call("get_messages", conversation_id="c1")
    assert thread["messages"][0]["direction"] == "received"
    assert all(request.method == "GET" for request in ka.sent("/messagebox/"))

    preview = await call("send_message", text="Ja", conversation_id="c1")
    assert preview["status"] == "confirmation_required"
    assert not any(request.method == "POST" for request in ka.requests)
    await call("send_message", text="Ja", conversation_id="c1", confirm=True)
    reply = [request for request in ka.requests if request.method == "POST"][-1]
    assert reply.url.path.endswith("/conversations/c1")
    assert reply.headers["authorization"] == "Bearer tok"
    assert b'"message":"Ja"' in reply.content or b'"message": "Ja"' in reply.content

    ka.on(API, "/api/users/42/conversations", httpx.Response(201, json={"id": "c9"}))
    opened = await call("send_message", text="Hallo", ad_id=7, contact_name="Max", confirm=True)
    assert opened["conversation_id"] == "c9"
    first = [request for request in ka.requests if request.url.host == API and request.method == "POST"][-1]
    assert b"contactName" in first.content and first.headers["authorization"].startswith("Basic ")
    with pytest.raises(ToolFailed, match="conversation_id to reply"):
        await call("send_message", text="Hallo", confirm=True)


async def test_a_failed_later_upload_reports_the_urls_already_stored(ka, call, tmp_path, monkeypatch):
    monkeypatch.setenv("KA_UPLOAD_ROOTS", str(tmp_path))
    first, second = tmp_path / "a.jpg", tmp_path / "b.jpg"
    first.write_bytes(b"\xff\xd8\xffa")
    second.write_bytes(b"\xff\xd8\xffb")
    calls = {"n": 0}

    def answer(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return picture("https://img.kleinanzeigen.de/api/v1/prod-ads/images/ab/one")
        return httpx.Response(500, text="<message>full</message>")

    ka.on(API, "/api/pictures.json", answer)
    with pytest.raises(ToolFailed, match="Already stored"):
        await call("upload_images", paths=[str(first), str(second)], confirm=True)
    assert calls["n"] == 2

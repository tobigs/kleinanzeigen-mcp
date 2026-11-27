"""Seller tools: your own ads and your own messages.

Reads and writes are separate functions. A write returns a preview and does not
call the mutating endpoint unless ``confirm`` is true. Field rules are read
from the public posting schema ``GET /api/ads/metadata/{category}.json`` rather
than a fixed list, because that document is what the API publishes and it
differs by category (Verschenken has no price; PC accessories require Art and
Versand).
"""

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlsplit
from xml.sax.saxutils import escape, quoteattr

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from . import account, api, categories, summaries


MAX_ROWS = 30
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_CHARS = 4000
PICTURE_HOST = "img.kleinanzeigen.de"

# Projection the app asks for on ad lists. Search works without it; the account
# list was not called live, so this asks for the same fields the summaries read.
OWNED_FIELDS = (
    "id,title,description,price,ad-status,ad-type,pictures,category.id,"
    "ad-address.state,ad-address.zip-code,user-id,seller-account-type,"
    "start-date-time,attributes,link"
)

# Friendly names a model writes, mapped onto the values the posting schema lists.
PRICE_ALIASES = {
    "SPECIFIED_AMOUNT": "SPECIFIED_AMOUNT",
    "FIXED": "SPECIFIED_AMOUNT",
    "FESTPREIS": "SPECIFIED_AMOUNT",
    "PLEASE_CONTACT": "PLEASE_CONTACT",
    "NEGOTIABLE": "PLEASE_CONTACT",
    "VB": "PLEASE_CONTACT",
    "FREE": "FREE",
    "GIVE_AWAY": "FREE",
}

_NAMESPACES = {
    "types": "types/v1",
    "cat": "category/v1",
    "ad": "ad/v1",
    "loc": "location/v1",
    "attr": "attribute/v1",
    "pic": "picture/v1",
}


def _as_list(node):
    if node is None:
        return []
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    return [node] if isinstance(node, dict) else []


def _enum_values(node: dict) -> dict:
    values = {}
    for item in _as_list(node.get("supported-value")):
        if item.get("value"):
            values[str(item["value"])] = item.get("localized-label") or str(item["value"])
    return values


def _dependencies(ad: dict) -> list:
    """Parent value -> allowed child values, from ``dependent-attributes``.

    A live phone schema names the child (``handy_telekom.model``) and then lists
    the models for one parent value per ``dependent-supported-value`` entry.
    The response we fetched only expanded Apple, so a parent with no entry is
    left unchecked rather than rejected.
    """
    found = []

    def collect(nodes, parent_name):
        for node in _as_list(nodes):
            parent = node.get("supported-value")
            child = node.get("dependent-attribute")
            if isinstance(parent, dict) and parent.get("value") and isinstance(child, dict) and child.get("name"):
                found.append({
                    "parent": parent_name,
                    "parent_value": str(parent["value"]),
                    "child": child["name"],
                    "child_write": child.get("write") or "optional",
                    "values": _enum_values(child),
                })
                collect(child.get("dependent-supported-value"), child["name"])

    for block in _as_list((ad.get("dependent-attributes") or {}).get("dependent-attribute")):
        collect(block.get("dependent-supported-value"), block.get("name"))
    return found


def parse_schema(payload: dict) -> dict:
    """The write rules in one posting-metadata document."""
    ad = api.collection(payload, "ad")
    attributes = []
    for attr in _as_list((ad.get("attributes") or {}).get("attribute")):
        if not attr.get("name"):
            continue
        attributes.append({
            "name": attr["name"],
            "label": attr.get("localized-label") or attr["name"],
            "type": attr.get("type") or "STRING",
            "write": attr.get("write") or "unsupported",
            "values": _enum_values(attr),
            "dependent_name": attr.get("dependent-name"),
        })
    price = ad.get("price") if isinstance(ad.get("price"), dict) else None
    price_on = bool(price) and price.get("write") == "required"
    location = (ad.get("locations") or {}).get("location")
    if isinstance(location, list):
        location = location[0] if location else {}
    return {
        "title_required": (ad.get("title") or {}).get("write") == "required",
        "description_required": (ad.get("description") or {}).get("write") == "required",
        "email_required": (ad.get("email") or {}).get("write") == "required",
        "price_required": price_on,
        "price_types": list(_enum_values((price or {}).get("price-type") or {})),
        "ad_types": list(_enum_values(ad.get("ad-type") or {})),
        "poster_types": list(_enum_values(ad.get("poster-type") or {})),
        "location_required": isinstance(location, dict) and location.get("write") == "required",
        "attributes": attributes,
        "dependencies": _dependencies(ad),
    }


def _values_hint(values: dict) -> str:
    keys = list(values)
    shown = ", ".join(keys[:12])
    if len(keys) > 12:
        shown += f", ... ({len(keys) - 12} more)"
    return shown or "a non-empty string"


def _line(name: str, value, *, required: bool) -> Optional[str]:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise ToolError(f"{name} is required.")
    if not text:
        return None
    if any(ch in text for ch in "\r\n\x00"):
        raise ToolError(f"{name} must be a single line.")
    return text


def _description(value, *, required: bool) -> Optional[str]:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise ToolError("description is required.")
    if not text:
        return None
    if "\x00" in text:
        raise ToolError("description contains a null character.")
    return text


def _amount(value) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ToolError(f"price must be a number (got {value!r}).") from None
    if not amount.is_finite() or amount < 0:
        raise ToolError("price must be zero or positive.")
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise ToolError("price can have at most two decimal places.")
    return f"{amount.quantize(Decimal('0.01'))}"


def _price_type(value, allowed: list) -> str:
    if not allowed:
        raise ToolError("This category's posting schema has no price. Leave price and price_type out.")
    if value is None:
        raise ToolError("price_type is required: " + ", ".join(allowed) + ".")
    mapped = PRICE_ALIASES.get(str(value).strip().upper().replace(" ", "_"))
    if mapped is None or mapped not in allowed:
        raise ToolError(f"price_type {value!r} is not one of: {', '.join(allowed)}.")
    return mapped


def _choice(name: str, value, allowed: list, default: Optional[str]) -> Optional[str]:
    chosen = default if value is None else str(value).strip().upper()
    if chosen is None:
        return None
    if allowed and chosen not in allowed:
        raise ToolError(f"{name} must be one of: {', '.join(allowed)}.")
    return chosen


def _picture_urls(urls) -> list:
    if not urls:
        return []
    if len(urls) > MAX_IMAGES:
        raise ToolError(f"At most {MAX_IMAGES} pictures (got {len(urls)}).")
    checked = []
    for url in urls:
        try:
            parts = urlsplit(str(url))
        except ValueError:
            parts = None
        if parts is None or parts.scheme != "https" or parts.hostname != PICTURE_HOST:
            raise ToolError(
                "picture_urls must be https links on img.kleinanzeigen.de, "
                "the URLs upload_images returns."
            )
        checked.append(str(url))
    return checked


def _check_attributes(schema: dict, provided: dict, *, partial: bool = False) -> tuple:
    """Return (checked attributes, names we could not check against a value list)."""
    known = {attr["name"]: attr for attr in schema["attributes"] if attr["write"] in {"required", "optional"}}
    children: dict = {}
    for rule in schema["dependencies"]:
        children.setdefault(rule["child"], []).append(rule)
    provided = {str(key): "" if value is None else str(value).strip() for key, value in provided.items()}
    if any(not value for value in provided.values()):
        raise ToolError("Attribute values cannot be empty.")

    for name, attr in known.items():
        if not partial and attr["write"] == "required" and name not in provided:
            raise ToolError(
                f"Attribute {attr['label']} ({name}) is required. Values: {_values_hint(attr['values'])}."
            )
        if name in provided and attr["values"] and provided[name] not in attr["values"]:
            raise ToolError(
                f"{provided[name]!r} is not a value of {name}. Values: {_values_hint(attr['values'])}."
            )
        if name in provided and attr["type"] not in {"ENUM", "STRING", "DECIMAL"}:
            raise ToolError(f"Attribute {name} has type {attr['type']}, which this server does not send.")

    unverified = []
    for name, value in provided.items():
        if name in known:
            continue
        rules = children.get(name) or []
        if not rules:
            known_names = ", ".join(list(known) + list(children)) or "none"
            raise ToolError(f"Unknown attribute {name!r}. This category accepts: {known_names}.")
        parent_name = rules[0]["parent"]
        if parent_name not in provided:
            raise ToolError(f"Set {parent_name} before {name}.")
        matched = [rule for rule in rules if rule["parent_value"] == provided[parent_name]]
        if not matched:
            unverified.append(name)
            continue
        allowed = matched[0]["values"]
        if allowed and value not in allowed:
            raise ToolError(
                f"{value!r} is not a {name} for {parent_name}={provided[parent_name]!r}. "
                f"Values: {_values_hint(allowed)}."
            )
    for rules in children.values():
        for rule in rules:
            if (rule["child_write"] == "required" and rule["parent"] in provided
                    and rule["parent_value"] == provided[rule["parent"]]
                    and rule["child"] not in provided):
                raise ToolError(
                    f"Attribute {rule['child']} is required when {rule['parent']} is "
                    f"{rule['parent_value']}. Values: {_values_hint(rule['values'])}."
                )
    return provided, unverified


def prepare_listing(schema: dict, *, title, description, category_id, location_id, price,
                    price_type, attributes, picture_urls, ad_type, poster_type, contact_name,
                    phone, email) -> dict:
    """Validate one new ad against ``schema`` and return the normalized fields."""
    if schema["email_required"] and not email:
        raise ToolError("This category requires an account email. Set KLEINANZEIGEN_USER_EMAIL.")
    if not schema["price_required"] and (price is not None or price_type is not None):
        raise ToolError("This category's posting schema has no price. Leave price and price_type out.")
    amount = None
    chosen_price = None
    if schema["price_required"]:
        chosen_price = _price_type(price_type, schema["price_types"])
        if chosen_price == "FREE":
            if price not in (None, 0, 0.0):
                raise ToolError("A FREE listing (zu verschenken) does not take a price.")
        else:
            if price is None:
                raise ToolError(f"price_type {chosen_price} needs a price.")
            amount = _amount(price)
    if schema["location_required"] and location_id is None:
        raise ToolError("location_id is required. Look one up with search_locations.")
    if location_id is not None and int(location_id) < 1:
        raise ToolError("location_id must be a positive id from search_locations.")
    checked, unverified = _check_attributes(schema, attributes or {})
    return {
        "title": _line("title", title, required=schema["title_required"]),
        "description": _description(description, required=schema["description_required"]),
        "category_id": str(category_id),
        "location_id": None if location_id is None else str(int(location_id)),
        "price_type": chosen_price,
        "price": amount,
        "attributes": checked,
        "unverified_attributes": unverified,
        "picture_urls": _picture_urls(picture_urls),
        "ad_type": _choice("ad_type", ad_type, schema["ad_types"], "OFFERED"),
        "poster_type": _choice("poster_type", poster_type, schema["poster_types"], "PRIVATE"),
        "contact_name": _line("contact_name", contact_name, required=False),
        "phone": _line("phone", phone, required=False),
        "email": email,
    }


def _xml(parts: list) -> str:
    ns = " ".join(
        f'xmlns:{prefix}="http://www.ebayclassifiedsgroup.com/schema/{path}"'
        for prefix, path in _NAMESPACES.items()
    )
    return "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>" + f"<ad:ad {ns}>" + "".join(parts) + "</ad:ad>"


def build_ad_xml(fields: dict) -> str:
    """The create body. Latitude and longitude are left out: the live schema
    marks them write=unsupported, and the location id carries the place."""
    parts = []
    if fields.get("title"):
        parts.append(f"<ad:title>{escape(fields['title'])}</ad:title>")
    if fields.get("description"):
        parts.append(f"<ad:description>{escape(fields['description'])}</ad:description>")
    if fields.get("contact_name"):
        parts.append(f"<ad:contact-name>{escape(fields['contact_name'])}</ad:contact-name>")
    if fields.get("email"):
        parts.append(f"<ad:email>{escape(fields['email'])}</ad:email>")
    if fields.get("phone"):
        parts.append(f"<ad:phone>{escape(fields['phone'])}</ad:phone>")
    if fields.get("poster_type"):
        parts.append(f"<ad:poster-type><ad:value>{escape(fields['poster_type'])}</ad:value></ad:poster-type>")
    if fields.get("ad_type"):
        parts.append(f"<ad:ad-type><ad:value>{escape(fields['ad_type'])}</ad:value></ad:ad-type>")
    parts.append(f"<cat:category id={quoteattr(fields['category_id'])}/>")
    if fields.get("location_id"):
        parts.append(
            f"<loc:locations><loc:location id={quoteattr(fields['location_id'])}/></loc:locations>"
        )
    if fields.get("price_type"):
        price = [f"<types:price-type><types:value>{escape(fields['price_type'])}</types:value></types:price-type>"]
        if fields.get("price") is not None:
            price.append(f"<types:amount>{escape(fields['price'])}</types:amount>")
        parts.append("<ad:price>" + "".join(price) + "</ad:price>")
    pictures = "".join(
        f"<pic:picture><pic:link rel=\"XXL\" href={quoteattr(url)}/></pic:picture>"
        for url in fields.get("picture_urls") or []
    )
    if fields.get("picture_urls"):
        parts.append(f"<pic:pictures>{pictures}</pic:pictures>")
    attrs = "".join(
        f"<attr:attribute name={quoteattr(key)}><attr:value>{escape(value)}</attr:value></attr:attribute>"
        for key, value in fields.get("attributes", {}).items()
    )
    if fields.get("attributes"):
        parts.append(f"<attr:attributes>{attrs}</attr:attributes>")
    return _xml(parts)


def build_update_xml(changes: dict) -> str:
    """Only the fields the caller passed. Whether omitted fields stay on the ad
    was not verified live, so the tool says so before it sends this."""
    parts = []
    for tag, key in (("title", "title"), ("description", "description")):
        if key in changes:
            parts.append(f"<ad:{tag}>{escape(changes[key])}</ad:{tag}>")
    if "price_type" in changes or "price" in changes:
        price = []
        if changes.get("price_type"):
            price.append(
                f"<types:price-type><types:value>{escape(changes['price_type'])}</types:value></types:price-type>"
            )
        if changes.get("price") is not None and changes.get("price_type") != "FREE":
            price.append(f"<types:amount>{escape(changes['price'])}</types:amount>")
        parts.append("<ad:price>" + "".join(price) + "</ad:price>")
    if "picture_urls" in changes:
        pictures = "".join(
            f"<pic:picture><pic:link rel=\"XXL\" href={quoteattr(url)}/></pic:picture>"
            for url in changes["picture_urls"]
        )
        parts.append(f"<pic:pictures>{pictures}</pic:pictures>")
    if "attributes" in changes:
        attrs = "".join(
            f"<attr:attribute name={quoteattr(key)}><attr:value>{escape(value)}</attr:value></attr:attribute>"
            for key, value in changes["attributes"].items()
        )
        parts.append(f"<attr:attributes>{attrs}</attr:attributes>")
    return _xml(parts)


def _hold(confirm: bool, action: str, preview: dict) -> Optional[dict]:
    if confirm is True:
        return None
    return {
        "status": "confirmation_required",
        "action": action,
        "confirm_with": "call again with confirm=true",
        **preview,
    }


def _ad_id(value) -> str:
    text = str(value).strip()
    if not text.isdigit():
        raise ToolError(f"ad id must be numeric (got {value!r}).")
    return text


def _path_id(value, name: str) -> str:
    text = str(value).strip()
    if not text or any(ch in text for ch in "/\\?&#"):
        raise ToolError(f"{name} has an unexpected character.")
    return text


async def _schema(category_id: int) -> dict:
    try:
        payload = await api.get(f"ads/metadata/{category_id}.json")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ToolError(
                f"No posting schema for category {category_id}. Use list_categories for an id."
            ) from exc
        raise
    return parse_schema(payload)


def _owned(ad: dict) -> dict:
    hit = summaries.summarize_ad_hit(ad)
    hit["ad_status"] = api.unwrap(ad.get("ad-status"))
    hit["ad_type"] = api.unwrap(ad.get("ad-type"))
    return hit


async def _owned_ad(user, ad_id: str) -> dict:
    payload = await api.get(f"users/{user.user_id}/ads/{ad_id}.json", account=user)
    return api.collection(payload, "ad")


def _category_of(ad: dict) -> int:
    category_id = (ad.get("category") or {}).get("id")
    if not str(category_id or "").isdigit():
        raise ToolError("The ad has no category id, so its posting schema cannot be loaded.")
    return int(category_id)


def _created_id(response: httpx.Response) -> Optional[str]:
    location = response.headers.get("location") or ""
    tail = location.rstrip("/").split("/")[-1].split(".")[0]
    if tail.isdigit():
        return tail
    if not response.content:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    ad = api.collection(payload, "ad") if isinstance(payload, dict) else {}
    ad_id = ad.get("id")
    if isinstance(ad_id, dict):
        ad_id = api.unwrap(ad_id)
    return str(ad_id) if str(ad_id or "").isdigit() else None


def _page(rows: int, offset: int) -> tuple:
    rows = int(rows)
    offset = int(offset)
    if rows < 1:
        raise ToolError(f"rows must be at least 1 (got {rows}).")
    if offset < 0:
        raise ToolError(f"offset must not be negative (got {offset}).")
    return min(rows, MAX_ROWS), offset


async def get_my_listings(rows: int = 25, offset: int = 0, query: Optional[str] = None) -> dict:
    """Your own ads, paused ones included. Needs your account token.

    Args:
        rows: At most 30.
        offset: Where to resume; pass the ``next_offset`` you got back.
        query: Optional keyword among your ads.
    """
    user = await account.require_account()
    rows, offset = _page(rows, offset)
    if offset % rows:
        raise ToolError(f"offset must be a multiple of rows ({rows}). Use next_offset.")
    page = offset // rows
    params = {"_in": OWNED_FIELDS, "page": page, "size": rows}
    if query:
        params["q"] = query
    payload = api.collection(
        await api.get(f"users/{user.user_id}/ads.json", params=params, account=user), "ads")
    ads = _as_list(payload.get("ad"))
    total = (payload.get("paging") or {}).get("numFound")
    total = int(total) if str(total or "").isdigit() else None
    position = offset + len(ads)
    more = position < total if total is not None else len(ads) == rows
    return {
        "total": total,
        "rows_returned": len(ads),
        "next_offset": position if more else None,
        "has_more": more,
        "results": [_owned(ad) for ad in ads],
    }


async def create_listing(
    title: str,
    description: str,
    category: Union[int, str],
    location_id: int,
    *,
    price: Optional[float] = None,
    price_type: Optional[str] = None,
    attributes: Optional[dict[str, str]] = None,
    picture_urls: Optional[list[str]] = None,
    ad_type: Optional[str] = None,
    poster_type: Optional[str] = None,
    contact_name: Optional[str] = None,
    phone: Optional[str] = None,
    confirm: bool = False,
) -> dict:
    """Publish one ad. Nothing is posted unless ``confirm`` is true.

    Required fields are whatever that category's posting schema marks required.
    ``price_type`` is SPECIFIED_AMOUNT, PLEASE_CONTACT or FREE when the category
    has a price. ``attributes`` maps a schema key to one value. ``picture_urls``
    come from ``upload_images``.

    Args:
        category: Category id or name.
        location_id: From ``search_locations``.
        confirm: True publishes the ad.
    """
    user = await account.require_account()
    category_id = categories.resolve(category)
    schema = await _schema(category_id)
    fields = prepare_listing(
        schema, title=title, description=description, category_id=category_id,
        location_id=location_id, price=price, price_type=price_type,
        attributes=attributes, picture_urls=picture_urls, ad_type=ad_type,
        poster_type=poster_type, contact_name=contact_name, phone=phone, email=user.email,
    )
    preview = {key: fields[key] for key in (
        "title", "category_id", "location_id", "price_type", "price", "attributes", "picture_urls")}
    if fields["unverified_attributes"]:
        preview["unverified_attributes"] = fields["unverified_attributes"]
    held = _hold(confirm, "create_listing", preview)
    if held:
        return held
    response = await api.send(
        "POST", f"{api.BASE_URL}/users/{user.user_id}/ads.json",
        content=build_ad_xml(fields).encode("utf-8"), content_type="application/xml; charset=utf-8",
        account=user,
    )
    ad_id = _created_id(response)
    result = {"status": "created" if ad_id else "created_unconfirmed", "id": ad_id, "url": summaries.ad_url(ad_id)}
    if not ad_id:
        result["warning"] = "The ad was accepted but no id came back. Check get_my_listings before posting again."
    if fields["unverified_attributes"]:
        result["unverified_attributes"] = fields["unverified_attributes"]
    return result


async def update_listing(
    ad_id: int,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    price: Optional[float] = None,
    price_type: Optional[str] = None,
    attributes: Optional[dict[str, str]] = None,
    picture_urls: Optional[list[str]] = None,
    confirm: bool = False,
) -> dict:
    """Change one of your ads. Only the fields you pass are sent, and nothing
    is sent unless ``confirm`` is true. Keeping the fields you leave out was
    not verified live, so read the ad afterwards.

    Args:
        ad_id: One of your ad ids.
        confirm: True sends the update.
    """
    user = await account.require_account()
    target = _ad_id(ad_id)
    changes = {}
    if title is not None:
        changes["title"] = _line("title", title, required=True)
    if description is not None:
        changes["description"] = _description(description, required=True)
    if picture_urls is not None:
        changes["picture_urls"] = _picture_urls(picture_urls)
    if (price is None) != (price_type is None):
        raise ToolError("Pass price and price_type together.")
    unverified = []
    if price is not None or attributes is not None:
        current = await _owned_ad(user, target)
        schema = await _schema(_category_of(current))
        if price is not None:
            if not schema["price_required"]:
                raise ToolError("This category's posting schema has no price.")
            chosen = _price_type(price_type, schema["price_types"])
            changes["price_type"] = chosen
            changes["price"] = None if chosen == "FREE" else _amount(price)
            if chosen == "FREE" and price not in (None, 0, 0.0):
                raise ToolError("A FREE listing (zu verschenken) does not take a price.")
        if attributes is not None:
            checked, unverified = _check_attributes(schema, attributes, partial=True)
            if not checked:
                raise ToolError("attributes is empty. Omit it to leave attributes unchanged.")
            changes["attributes"] = checked
    if not changes:
        raise ToolError("Pass at least one of title, description, price, attributes, picture_urls.")
    preview = {"id": target, "changes": sorted(changes), "keeps_omitted_fields": "not verified live"}
    if unverified:
        preview["unverified_attributes"] = unverified
    held = _hold(confirm, "update_listing", preview)
    if held:
        return held
    await api.send(
        "PUT", f"{api.BASE_URL}/users/{user.user_id}/ads/{target}.json",
        content=build_update_xml(changes).encode("utf-8"), content_type="application/xml; charset=utf-8",
        account=user,
    )
    result = {"status": "updated", "id": target, "fields": sorted(changes)}
    if unverified:
        result["unverified_attributes"] = unverified
    return result


async def _state_change(ad_id: int, state: str, action: str, confirm: bool) -> dict:
    user = await account.require_account()
    target = _ad_id(ad_id)
    current = await _owned_ad(user, target)
    preview = {
        "id": target,
        "title": summaries.summarize_ad_hit(current).get("title"),
        "ad_status": api.unwrap(current.get("ad-status")),
        "next_status": state,
    }
    held = _hold(confirm, action, preview)
    if held:
        return held
    # The live routes are PUT .../ads/paused/{id}.json and .../ads/active/{id}.json.
    slug = "paused" if state == "PAUSED" else "active"
    await api.send("PUT", f"{api.BASE_URL}/users/{user.user_id}/ads/{slug}/{target}.json", account=user)
    return {"status": state.lower(), "id": target}


async def pause_listing(ad_id: int, *, confirm: bool = False) -> dict:
    """Take one of your ads offline. Reversible with ``activate_listing``.
    Nothing is sent unless ``confirm`` is true.

    Args:
        ad_id: One of your ad ids.
        confirm: True pauses the ad.
    """
    return await _state_change(ad_id, "PAUSED", "pause_listing", confirm)


async def activate_listing(ad_id: int, *, confirm: bool = False) -> dict:
    """Put a paused ad back online. Nothing is sent unless ``confirm`` is true.

    Args:
        ad_id: One of your ad ids.
        confirm: True activates the ad.
    """
    return await _state_change(ad_id, "ACTIVE", "activate_listing", confirm)


async def delete_listing(ad_id: int, *, confirm: bool = False) -> dict:
    """Permanently delete one of your ads. Nothing is sent unless ``confirm``
    is true, and a deleted ad cannot be activated again.

    Args:
        ad_id: One of your ad ids.
        confirm: True deletes the ad.
    """
    user = await account.require_account()
    target = _ad_id(ad_id)
    current = await _owned_ad(user, target)
    preview = {
        "id": target,
        "title": summaries.summarize_ad_hit(current).get("title"),
        "permanent": True,
    }
    held = _hold(confirm, "delete_listing", preview)
    if held:
        return held
    await api.send("DELETE", f"{api.BASE_URL}/users/{user.user_id}/ads/{target}", account=user)
    return {"status": "deleted", "id": target}


def _image_type(head: bytes) -> Optional[str]:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _upload_file(path: str) -> tuple:
    file_path = Path(path).expanduser()
    try:
        resolved = file_path.resolve(strict=True)
    except OSError as exc:
        raise ToolError(f"Cannot read image {path!r}.") from exc
    roots = [Path(item).expanduser().resolve() for item in os.environ.get("KA_UPLOAD_ROOTS", "").split(os.pathsep) if item]
    if roots and not any(resolved == root or root in resolved.parents for root in roots):
        raise ToolError("That image is outside KA_UPLOAD_ROOTS.")
    if not resolved.is_file():
        raise ToolError(f"{path!r} is not a file.")
    size = resolved.stat().st_size
    if size < 1 or size > MAX_IMAGE_BYTES:
        raise ToolError(f"{resolved.name} must be between 1 byte and 8 MB.")
    head = resolved.read_bytes()[:16]
    kind = _image_type(head)
    if kind is None:
        raise ToolError(f"{resolved.name} is not a JPEG, PNG, GIF or WebP image.")
    return resolved, kind


def _picture_href(payload: dict) -> str:
    picture = api.collection(payload, "picture") if isinstance(payload, dict) else {}
    links = picture.get("link") or []
    if isinstance(links, dict):
        links = [links]
    by_rel = {link.get("rel"): link.get("href") for link in links if isinstance(link, dict) and link.get("href")}
    url = by_rel.get("XXL") or by_rel.get("extraLarge") or by_rel.get("large") or next(iter(by_rel.values()), None)
    if not isinstance(url, str) or not url:
        raise ToolError("The picture was stored but the response had no image URL. Do not upload it again blindly.")
    return url


async def upload_images(paths: list[str], *, confirm: bool = False) -> dict:
    """Upload JPEG, PNG, GIF or WebP files and return their hosted URLs for
    ``create_listing``. The upload itself needs no user token. Nothing is sent
    unless ``confirm`` is true. Set ``KA_UPLOAD_ROOTS`` to limit which
    directories may be read.

    Args:
        paths: Local image files, at most 10.
        confirm: True uploads them.
    """
    if not paths:
        raise ToolError("Pass at least one image path.")
    if len(paths) > MAX_IMAGES:
        raise ToolError(f"At most {MAX_IMAGES} images per call (got {len(paths)}).")
    files = [_upload_file(path) for path in paths]
    preview = {"files": [{"name": path.name, "content_type": kind} for path, kind in files]}
    held = _hold(confirm, "upload_images", preview)
    if held:
        return held
    uploaded = []
    for path, kind in files:
        try:
            response = await api.send(
                "POST", f"{api.BASE_URL}/pictures.json",
                files={"file": (path.name, path.read_bytes(), kind)},
            )
            uploaded.append({"name": path.name, "url": _picture_href(response.json())})
        except httpx.HTTPStatusError as exc:
            message = api.upstream_message(exc.response.status_code, "picture", exc.response.text)
            if uploaded:
                kept = ", ".join(item["url"] for item in uploaded)
                message = f"{message} Already stored (do not upload these again): {kept}"
            raise ToolError(message) from exc
        except httpx.HTTPError as exc:
            message = f"could not reach Kleinanzeigen for the picture upload ({type(exc).__name__})"
            if uploaded:
                kept = ", ".join(item["url"] for item in uploaded)
                message = f"{message} Already stored (do not upload these again): {kept}"
            raise ToolError(message) from exc
        except ToolError as exc:
            if not uploaded:
                raise
            kept = ", ".join(item["url"] for item in uploaded)
            raise ToolError(f"{exc} Already stored (do not upload these again): {kept}") from exc
    return {"status": "uploaded", "images": uploaded}


def _conversations(body: dict) -> list:
    items = body.get("conversations") if isinstance(body, dict) else None
    if items is None and isinstance(body, dict):
        data = body.get("data")
        items = data if isinstance(data, list) else (data or {}).get("conversations")
    if isinstance(items, dict):
        items = items.get("conversations") or items.get("items")
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").upper()
        other = item.get("sellerName") if role == "BUYER" else item.get("buyerName")
        out.append({
            "id": str(item.get("id") or ""),
            "ad_id": str(item.get("adId") or "") or None,
            "ad_title": item.get("adTitle") or None,
            "role": role or None,
            "counterparty": other or item.get("sellerName") or item.get("buyerName") or None,
            "unread": bool(item.get("unread")),
            "unread_count": item.get("unreadMessagesCount"),
            "preview": item.get("textShortTrimmed") or None,
            "received": item.get("receivedDate") or None,
        })
    return out


def _thread(body: dict) -> list:
    raw = []
    if isinstance(body, dict):
        raw = body.get("messages") or []
        if not raw and isinstance(body.get("data"), dict):
            raw = body["data"].get("messages") or []
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        bound = str(item.get("boundness") or item.get("direction") or "").upper()
        if "IN" in bound:
            direction = "received"
        elif "OUT" in bound:
            direction = "sent"
        else:
            direction = bound.lower() or None
        out.append({
            "text": item.get("text") or item.get("textShort") or item.get("title") or "",
            "direction": direction,
            "date": item.get("receivedDate") or None,
        })
    return out


async def get_messages(conversation_id: Optional[str] = None, *, rows: int = 20, page: int = 0) -> dict:
    """Your inbox, or one thread when ``conversation_id`` is set. Reading a
    thread uses GET. It does not use the PUT that other clients use to open a
    thread, because that PUT also marks the thread loaded.

    Args:
        conversation_id: One thread. Omit to list the inbox.
        rows: Inbox page size, at most 30.
        page: Inbox page, from 0.
    """
    user = await account.require_account()
    if conversation_id:
        target = _path_id(conversation_id, "conversation_id")
        payload = await api.get_gateway(
            f"messagebox/api/users/{user.user_id}/conversations/{target}", account=user)
        return {"id": target, "messages": _thread(payload if isinstance(payload, dict) else {})}
    rows, _ = _page(rows, 0)
    if int(page) < 0:
        raise ToolError(f"page must not be negative (got {page}).")
    payload = await api.get_gateway(
        f"messagebox/api/users/{user.user_id}/conversations",
        params={"page": int(page), "size": rows}, account=user)
    messages = _conversations(payload if isinstance(payload, dict) else {})
    return {"page": int(page), "count": len(messages), "conversations": messages}


def _message_text(text: str) -> str:
    body = "" if text is None else str(text).strip()
    if not body:
        raise ToolError("text is required.")
    if "\x00" in body:
        raise ToolError("text contains a null character.")
    # No published maximum. 4000 is this client's guard, not a schema limit.
    if len(body) > MAX_MESSAGE_CHARS:
        raise ToolError(f"text is longer than {MAX_MESSAGE_CHARS} characters.")
    return body


async def send_message(
    text: str,
    *,
    conversation_id: Optional[str] = None,
    ad_id: Optional[int] = None,
    contact_name: Optional[str] = None,
    confirm: bool = False,
) -> dict:
    """Reply in a thread, or write the first message on an ad. Nothing is sent
    unless ``confirm`` is true. Messages that contain a phone number or email
    may be refused by Kleinanzeigen; that refusal is returned as-is.

    Args:
        text: The message.
        conversation_id: Reply in this thread.
        ad_id: Start a thread on this ad instead of replying.
        contact_name: Required with ``ad_id``. Your name on the message.
        confirm: True sends the message.
    """
    user = await account.require_account()
    body = _message_text(text)
    if (conversation_id is None) == (ad_id is None):
        raise ToolError("Pass conversation_id to reply, or ad_id to contact the seller of that ad.")
    if conversation_id is not None:
        target = _path_id(conversation_id, "conversation_id")
        preview = {"to": "conversation", "conversation_id": target, "text": body}
        held = _hold(confirm, "send_message", preview)
        if held:
            return held
        await api.send(
            "POST",
            f"{api.GATEWAY_URL}/messagebox/api/users/{user.user_id}/conversations/{target}",
            json_body={"message": body}, account=user,
        )
        return {"status": "sent", "conversation_id": target}
    target = _ad_id(ad_id)
    name = _line("contact_name", contact_name, required=True)
    preview = {"to": "ad", "ad_id": target, "contact_name": name, "text": body}
    held = _hold(confirm, "send_message", preview)
    if held:
        return held
    response = await api.send(
        "POST", f"{api.BASE_URL}/users/{user.user_id}/conversations",
        json_body={"message": body, "adId": int(target), "contactName": name}, account=user,
    )
    conversation = None
    if response.content:
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            payload = None
        if isinstance(payload, dict):
            conversation = payload.get("id") or (payload.get("conversation") or {}).get("id")
    return {
        "status": "sent",
        "ad_id": target,
        "conversation_id": str(conversation) if conversation else None,
    }

"""Posting rules taken from a category schema, with no network."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from kleinanzeigen import seller
from samples import posting_metadata

PC = posting_metadata(attributes=[
    ("pc_zubehoer_software.art", "required", [("grafikkarten", "Grafikkarten"), ("mainboards", "Mainboards")]),
    ("pc_zubehoer_software.versand", "required", [("ja", "Ja"), ("nein", "Nein")]),
    ("pc_zubehoer_software.condition", "optional", [("neu", "Neu")]),
])
GIFT = posting_metadata(price=False)
PHONE = posting_metadata(attributes=[
    ("phones.brand", "required", [("apple", "Apple"), ("samsung", "Samsung")]),
], dependencies=[{
    "name": "phones.brand",
    "dependent-name": "phones.model",
    "write": "required",
    "dependent-supported-value": [{
        "supported-value": {"value": "apple", "localized-label": "Apple"},
        "dependent-attribute": {
            "name": "phones.model", "write": "optional", "type": "ENUM",
            "supported-value": [{"value": "iphone_16", "localized-label": "iPhone 16"}],
        },
    }],
}])


def fields(schema, **overrides):
    base = dict(title="Grafikkarte", description="Funktionstüchtig", category_id=225, location_id=3331,
                price=12.5, price_type="FIXED", attributes={
                    "pc_zubehoer_software.art": "grafikkarten",
                    "pc_zubehoer_software.versand": "ja",
                }, picture_urls=None, ad_type=None, poster_type=None, contact_name=None, phone=None,
                email="a@b.de")
    base.update(overrides)
    return seller.prepare_listing(seller.parse_schema(schema), **base)


def test_a_category_schema_requires_its_attributes_and_normalizes_price():
    prepared = fields(PC)
    assert prepared["price"] == "12.50"
    assert prepared["price_type"] == "SPECIFIED_AMOUNT"
    xml = seller.build_ad_xml(prepared)
    assert "grafikkarten" in xml and 'id="225"' in xml and "a@b.de" in xml
    assert "latitude" not in xml and "FIXED" not in xml


def test_title_markup_is_escaped():
    prepared = fields(PC, title="A & B <c>")
    assert "A &amp; B &lt;c&gt;" in seller.build_ad_xml(prepared)


def test_missing_required_attribute_names_the_allowed_values():
    with pytest.raises(ToolError, match="pc_zubehoer_software.versand"):
        fields(PC, attributes={"pc_zubehoer_software.art": "grafikkarten"})


def test_an_unknown_attribute_value_is_rejected():
    with pytest.raises(ToolError, match="not a value"):
        fields(PC, attributes={
            "pc_zubehoer_software.art": "drucker",
            "pc_zubehoer_software.versand": "ja",
        })


def test_a_category_without_a_price_rejects_one():
    schema = seller.parse_schema(GIFT)
    assert schema["price_required"] is False
    with pytest.raises(ToolError, match="no price"):
        fields(GIFT, price=0, price_type="FREE", attributes={})


def test_free_omits_the_amount_and_a_priced_free_listing_is_rejected():
    prepared = fields(PC, price=None, price_type="FREE")
    assert prepared["price"] is None
    assert "<types:amount>" not in seller.build_ad_xml(prepared)
    with pytest.raises(ToolError, match="does not take a price"):
        fields(PC, price=5, price_type="FREE")


def test_picture_urls_must_be_on_the_image_host():
    with pytest.raises(ToolError, match="img.kleinanzeigen.de"):
        fields(PC, picture_urls=["https://example.com/a.jpg"])


def test_a_dependent_model_is_checked_only_when_the_schema_lists_it():
    ok = fields(PHONE, attributes={"phones.brand": "apple", "phones.model": "iphone_16"})
    assert ok["unverified_attributes"] == []
    unknown = fields(PHONE, attributes={"phones.brand": "samsung", "phones.model": "galaxy"})
    assert unknown["unverified_attributes"] == ["phones.model"]
    with pytest.raises(ToolError, match="iphone_16"):
        fields(PHONE, attributes={"phones.brand": "apple", "phones.model": "galaxy"})


def test_an_update_sends_only_the_fields_it_was_given():
    xml = seller.build_update_xml({"title": "Neu", "picture_urls": []})
    assert "<ad:title>Neu</ad:title>" in xml
    assert "<pic:pictures></pic:pictures>" in xml
    assert "<ad:price>" not in xml and "<loc:" not in xml

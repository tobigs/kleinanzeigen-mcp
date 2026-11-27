"""Canned Kleinanzeigen API responses, trimmed to the fields the server reads
but shaped like the live ones (JAXB-wrapped, see search_api.md), so the suite
runs offline."""

AD_NS = "{http://www.ebayclassifiedsgroup.com/schema/ad/v1}"
LOCATION_NS = "{http://www.ebayclassifiedsgroup.com/schema/location/v1}"


def v(value):
    return {"value": value}


def wrapped(name, value, ns=AD_NS):
    return {f"{ns}{name}": {"name": f"{ns}{name}", "value": value}}


def attribute(name, label, value, value_label):
    return {"name": name, "localized-label": label, "type": "ENUM",
            "value": [{"value": value, "localized-label": value_label}]}


def ad(ad_id, title, *, price=150, price_type="SPECIFIED_AMOUNT", description="Funktioniert einwandfrei.",
       top=False, images=2, shipping=("DHL_001",), seller_id="22075716", attributes=(), category="173"):
    return {
        "id": str(ad_id),
        "title": v(title),
        "description": v(description),
        "price": {"currency-iso-code": v(v("EUR")), "amount": v(price), "price-type": v(price_type)},
        "ad-type": v("OFFERED"),
        "ad-status": v("ACTIVE"),
        "ad-address": {"state": v("Hohenlinden"), "zip-code": v("85664"),
                       "latitude": v("48.15748"), "longitude": v("11.987107")},
        "user-id": v(seller_id),
        "store-title": {},
        "seller-account-type": v("PRIVATE"),
        "contact-name": v("Max"),
        "user-rating": {"averageRating": v(4.5)},
        "start-date-time": v("2026-10-27T23:54:24.000+0100"),
        "features-active": {"feature-active": [{"name": "TOPAD"}] if top else []},
        "category": {"id": category, "localized-name": v("Smartphones")},
        "shipping-options": {"shipping-option": [{"id": s} for s in shipping]},
        "buy-now": {"selected": "false"},
        "attributes": {"attribute": list(attributes)},
        "pictures": {"picture": [
            {"link": [{"href": f"https://img.kleinanzeigen.de/api/v1/prod-ads/images/{ad_id}-{i}?rule=$_1.AUTO",
                       "rel": "large"},
                      {"href": f"https://img.kleinanzeigen.de/api/v1/prod-ads/images/{ad_id}-{i}?rule=$_59.AUTO",
                       "rel": "extraLarge"}]} for i in range(images)]},
        "link": [{"href": f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}-{category}-1234",
                  "rel": "self-public-website"}],
    }


def search(ads, *, total=None):
    return wrapped("ads", {"ad": ads, "paging": {"numFound": str(len(ads) if total is None else total)}})


def detail(*args, **kwargs):
    return wrapped("ad", ad(*args, **kwargs))


def locations(*names):
    return wrapped("locations", {"location": [
        {"id": str(3331 + i), "localized-name": v(name), "latitude": v("52.5"), "longitude": v("13.4"),
         "radius": v("15.35"), "regions": {"region": [{"localized-name": v("Berlin")}]}}
        for i, name in enumerate(names)]}, ns=LOCATION_NS)


def search_options(*attributes):
    return wrapped("ads-search-options", {"attributes": {"attribute": list(attributes)}})


def enum_filter(name, label, *values, multi=True):
    return {"name": name, "localized-label": label, "type": "ENUM",
            "search-multi-select": "true" if multi else "false",
            "supported-value": [{"value": value, "localized-label": value_label} for value, value_label in values]}


def posting_metadata(*, attributes=(), price=True, dependencies=()):
    """A posting schema shaped like ``GET /api/ads/metadata/{id}.json``."""
    def field(write, type_="STRING", values=None):
        node = {"write": write, "type": type_}
        if values is not None:
            node["supported-value"] = [{"value": value, "localized-label": label} for value, label in values]
        return node

    attrs = [{
        "name": name, "localized-label": name.split(".")[-1], "type": "ENUM", "write": write,
        "supported-value": [{"value": value, "localized-label": label} for value, label in values],
    } for name, write, values in attributes]
    ad = {
        "title": field("required"),
        "description": field("required"),
        "email": field("required"),
        "category": {"write": "required"},
        "ad-type": field("optional", "ENUM", [("OFFERED", "Angebot"), ("WANTED", "Gesuch")]),
        "poster-type": field("optional", "ENUM", [("PRIVATE", "Privat"), ("COMMERCIAL", "Gewerblich")]),
        "locations": {"location": [{"write": "required", "type": "LONG"}]},
        "attributes": {"attribute": attrs},
    }
    if price:
        ad["price"] = {
            "write": "required",
            "amount": field("optional", "DECIMAL"),
            "price-type": field("required", "ENUM", [
                ("FREE", "Zu verschenken"), ("PLEASE_CONTACT", "VB"), ("SPECIFIED_AMOUNT", "Festpreis")]),
        }
    if dependencies:
        ad["dependent-attributes"] = {"dependent-attribute": list(dependencies)}
    return wrapped("ad", ad)


def picture(url):
    ns = "{http://www.ebayclassifiedsgroup.com/schema/picture/v1}"
    return {f"{ns}picture": {"value": {"link": [
        {"rel": "large", "href": url + "?rule=small"},
        {"rel": "XXL", "href": url},
    ]}}}


def profile(seller_id="22075716"):
    return {"id": seller_id, "contactName": "Max", "posterType": "PRIVATE", "userSince": "2015-03-01",
            "replyIndicators": {"replySpeed": "within an hour"},
            "counters": {"onlineAds": 3, "historicalAds": 40, "followers": 2},
            "userRatings": {"averageRating": 2}}


def reputation():
    return {"total": {"averageRating": 4.9, "ratingReviewCount": 17}}

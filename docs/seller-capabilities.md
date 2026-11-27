# Seller capabilities

Probed on 2026-11-27 from this repository's httpx client (`kleinanzeigen/api.py`),
using the app Basic credential the search tools already send. No password was
submitted, no CAPTCHA was solved, and no browser fingerprint was imitated.
Account routes were called without a user token, or with the obviously invalid
token `invalid`, so the server would reject them before acting. One 1×1 JPEG
was posted to the picture endpoint during that probe; it was not attached to
an ad. No listing was created, changed, paused, deleted, or messaged.

Status codes below are from that probe. "Partial" means the route exists, and
the part we could not watch succeed with a real account is named in the
limitation column.

## Capability matrix

| Tool | Support | Auth | Evidence | Limitation |
|---|---|---|---|---|
| `get_my_listings` | partial | user token | `GET /api/users/{id}/ads.json` → 401, "Authentication is mandatory for /users resource" | Not called with a real token. The response shape is the same ads collection search uses. The request asks for the app's `_in` field list so paused ads still carry status, price and pictures |
| `create_listing` | partial | user token | `POST /api/users/{id}/ads.json` → 401. `GET /api/ads/metadata/{category}.json` → 200 for categories 192, 225 and 173 | The XML body follows that schema and was not submitted live, so a 2xx create is not confirmed |
| `update_listing` | partial | user token | `PUT /api/users/{id}/ads/{adId}.json` → 401 | Only the fields you pass are sent. Whether Kleinanzeigen keeps the ones you leave out was not verified. Read the ad after an update |
| `delete_listing` | partial | user token | `DELETE /api/users/{id}/ads/{adId}` → 401 | The route exists. A real delete was not performed. `confirm=true` is required, and the tool reads the ad first so the preview shows its title |
| `pause_listing` | partial | user token | `PUT /api/users/{id}/ads/paused/{adId}.json` → 401 | The route exists. A real pause was not performed |
| `activate_listing` | partial | user token | `PUT /api/users/{id}/ads/active/{adId}.json` → 401 | The route exists. A real activation was not performed |
| `upload_images` | supported | app Basic credential | `POST /api/pictures.json` with a JPEG → 201 and a `{...}picture` document whose `link[].href` is on `img.kleinanzeigen.de`. An empty body → 500, not 404 | No user token was required. The probe stored one tiny JPEG that belongs to no ad. Non-images are rejected locally |
| `get_messages` | partial | user token | `GET /messagebox/api/users/{id}/conversations` → 401. `GET .../conversations/{id}` → 401 | Both a missing token and a wrong token came back as an empty 401, so which header the gateway checks was not distinguishable. This tool uses GET. It does not call the PUT other clients use to open a thread, because that PUT also marks the thread loaded |
| `send_message` | partial | user token | `POST .../conversations/{id}` on the gateway → 401. `POST /api/users/{id}/conversations` → 401 | Bodies were not submitted. The first-contact body is `{"message", "adId", "contactName"}`, which is the shape published app clients send. Query flags that suppress phone-number and email warnings are not sent, so Kleinanzeigen can still refuse those messages |

Login, which the tools need and which is not itself one of the ten tools:

| Step | Support | Evidence |
|---|---|---|
| Auth0 authorize URL | supported | `GET https://login.kleinanzeigen.de/authorize` with the app's public client id → 302 to the real login page (`/u/login/identifier`). OpenID configuration → 200 |
| Token exchange | supported as a normal OAuth code exchange | `POST https://login.kleinanzeigen.de/oauth/token`. Tested against a fake server. Not exercised with a real code in this probe |
| Password login | not supported | The API challenge also allows a password (`supported-secret="password,token"`). This server never sends one |

## What a signed-in call sends

On `api.kleinanzeigen.de` the user header is:

```
X-ECG-Authorization-User: email=<email>,access=<token>
```

A live call with `email=nobody@example.com,access=invalid` changed the error
from "authentication is mandatory" to "the passed authentication does not match
the user in the URL". The same call with only `x-ebayk-userid-token: invalid`
stayed on "authentication is mandatory", so that header is not sufficient.

On the message gateway the client sends `Authorization: Bearer <token>` and the
same user header. See the matrix for why that pair is still partial.

The numeric user id has to be the account that owns the token.
`GET /api/users/{email}/profile.json` is the lookup (401 without a token, "unknown
user" for a token that does not belong to that email). `python -m kleinanzeigen.login`
does this lookup and stores the id.

## How create decides which fields are required

`GET /api/ads/metadata/{categoryId}.json` is public and is the source of the
rules. Checked live:

- Title, description, email and category are `write=required`.
- Location's own node is `write=required` (type LONG). Latitude, longitude,
  city and postcode are `write=unsupported`, so the XML sends the location id
  and not coordinates.
- Price is `write=required` on categories that have a price (225 PC accessories,
  173 phones) and is absent on 192 Verschenken. Price types published there are
  `FREE`, `PLEASE_CONTACT`, `SPECIFIED_AMOUNT`. Amount is marked optional because
  a free listing has none; this server still requires an amount for the other
  two types.
- Attributes with `write=required` must be present and, for an ENUM, one of
  `supported-value`. Optional attributes may be omitted. Anything marked
  `write=unsupported` is rejected.
- Dependent attributes (phone model under phone brand) are checked when the
  metadata response includes that parent value. The phone document returned
  models for Apple and no other brand. A model for a brand the document does
  not expand is sent and reported as `unverified_attributes` instead of being
  invented or rejected.
- The schema publishes no maximum length for title, description or message
  text. Empty values are rejected. Message text is also capped at 4000
  characters by this client, because the API did not publish a limit.

`picture_urls` have to be `https` links on `img.kleinanzeigen.de`, which is
where `upload_images` points.

## Read and write

`get_my_listings` and `get_messages` are read-only. Every other account tool
returns `status: "confirmation_required"` and does not call the mutating
endpoint unless `confirm` is true. Delete, pause and activate read the ad
first so the preview names it. Create and update read the posting schema first
so an invalid attribute fails before confirm matters.

POST is not retried. A retry can publish a second ad, send a second message,
or store a second picture. GET, PUT and DELETE retry timeouts and HTTP 429/5xx,
with the same backoff search already uses. Every call, including those retries,
waits at least 0.4 seconds after the previous one.

Public search does not attach the user token, even when the environment has one.

## Not supported, on purpose

- Another person's ads, drafts, or messages. The user id in the URL is always
  the signed-in account, and the server rejected a token that did not match
  that id.
- Password login, CAPTCHA solving, and TLS or browser impersonation. If a
  network blocks this client, the call fails.
- A browser bot. None of the ten tools was a 404, so there is no gap here that
  a browser has to fill. If a route disappears later, the person can do that
  one action on kleinanzeigen.de themselves. An automated browser would still
  have to stop at a login challenge or a CAPTCHA, and this repository does not
  ship one.
- Renewing an ad (`/ads/extend`) and the watchlist. They were not part of the
  requested tools, and extend eligibility was not probed.
- Suppressing Kleinanzeigen's warnings when a message contains a phone number,
  an email address, or bank details.

## Operational notes

Automating a logged-in Kleinanzeigen account can conflict with their terms and
can get the account limited. Keep the volume personal. The refresh token in
`~/.kleinanzeigen-mcp/token.json` is a password; the file is mode 0600.
`KLEINANZEIGEN_ACCESS_TOKEN`, `KLEINANZEIGEN_USER_EMAIL`, `KLEINANZEIGEN_USER_ID`
and `KLEINANZEIGEN_REFRESH_TOKEN` override that file.
`KA_UPLOAD_ROOTS` limits which directories `upload_images` may read.

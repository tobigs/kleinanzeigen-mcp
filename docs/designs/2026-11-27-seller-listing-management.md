# Seller listing management

- **Complexity:** High
- **Design Model:** This session, from the owner's brief (the brief is the product contract)
- **Build Model:** This session
- **Status:** Implemented

## 1. Executive Summary (User Approval Contract)

- **Problem Statement:** The server can search Kleinanzeigen and read public ads. It cannot show or change the ads and messages of the account that is signed in.
- **Proposed Product Behavior:** Ten account tools: list your ads, create, update, pause, activate and delete one, upload pictures, and read or send messages. A write returns a preview until the call passes `confirm=true`. Sign-in is the normal browser login. The server never types a password and never tries to get past a CAPTCHA.
- **Scope & Non-Goals:** Your own account only. No renewal, no watchlist, no browser automation, no action on someone else's ads. Where a live signed-in success was not observed, the tool still calls the route that answered 401 and the limitation is documented.
- **Product-Level Trade-offs:** Update sends only the fields you name, and it is not yet proven that Kleinanzeigen keeps the rest. Message sending does not silence the site's phone and email warnings, so some messages will be refused. Picture upload works with the app credential alone, which is how the API behaved.

## 2. Product-Level Specifications

The person signs in with `python -m kleinanzeigen.login` or sets
`KLEINANZEIGEN_ACCESS_TOKEN`, `KLEINANZEIGEN_USER_EMAIL` and
`KLEINANZEIGEN_USER_ID`. Listing fields are checked against the public posting
schema for that category before anything is published. Missing required
attributes, unknown enum values, a price on a category that has no price, and
picture URLs off the image host are rejected with the allowed values named.
Destructive and other writes do nothing until `confirm` is true. Delete says
the action is permanent. The capability matrix in `docs/seller-capabilities.md`
is the list of what was verified.

## 3. Technical Architecture & System Boundaries

- `kleinanzeigen/api.py` keeps the app credential, adds the account header, paces every call, and retries only idempotent methods.
- `kleinanzeigen/account.py` reads the env or the token file, refreshes an Auth0 token, and resolves the numeric user id from `GET /users/{email}/profile.json`.
- `kleinanzeigen/login.py` builds the authorize URL and exchanges the code the browser landed on. The verifier stays in the token file, not in the chat.
- `kleinanzeigen/seller.py` parses posting metadata, builds the ad XML, and implements the ten tools. `kleinanzeigen/server.py` registers them and marks reads and writes with different tool hints.
- Account calls on the API host send `X-ECG-Authorization-User: email=<email>,access=<token>` plus the existing Basic credential. The message gateway sends `Authorization: Bearer` and the same user header.
- Create and update XML uses the eBay Classifieds namespaces. Coordinates are omitted because the schema marks them write-unsupported.

## 4. Autonomous Decisions & Trade-Offs

- Confirmation is a `confirm` argument defaulting to false, so a model cannot mutate by calling the tool once. The MCP `destructiveHint` is set as well, for clients that ask on their own.
- POST is not retried. GET, PUT and DELETE are. A minimum interval of 0.4 seconds is shared with search.
- Dependent attribute values are enforced only for parent values the metadata document actually expands. Rejecting every other brand would contradict the document we fetched, which expanded Apple and not Samsung.
- The message gateway's successful auth header could not be told apart (empty 401 either way). The client sends both headers the rest of the API family uses, and the matrix says so.
- Browser automation was investigated as a fallback and not built. Every requested operation had a routed endpoint (401 or, for pictures and metadata, 2xx). Shipping a browser would not add a missing operation and would sit next to the CAPTCHA this server is required to leave alone.
- TLS impersonation was not added. The existing httpx client already received 200 from search and from posting metadata on this network.

## 5. Verification, Testing & Rollback Plan

- Offline: `python3 -m pytest -q` (live tests skip unless `KLEINANZEIGEN_LIVE=1`). Covers each tool, the confirm gate, schema validation, auth headers, retry versus no-retry, upload type checks, and the login code exchange against a fake Auth0.
- Live, read-only: `KLEINANZEIGEN_LIVE=1 python3 -m pytest -q tests/test_live.py`. Includes the existing search smoke and a check that `GET /users/1/ads.json` is 401 and that category 225's posting schema has required attributes. Passed on 2026-11-27.
- Rollback: revert the branch. No data migration. Tokens live outside the repo.

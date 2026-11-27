"""One-time browser login. Run ``python -m kleinanzeigen.login``.

Opens nothing by itself unless ``--open`` is passed. The browser, and any
CAPTCHA on the login page, stay with the person. The code in the final URL is
exchanged for a token and stored in ``~/.kleinanzeigen-mcp/token.json``.
"""

import argparse
import asyncio
import base64
import hashlib
import secrets
import webbrowser

from . import account


def _pkce() -> tuple:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    state = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")
    return verifier, challenge, state


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Sign in to Kleinanzeigen for the seller tools")
    parser.add_argument("--open", action="store_true", help="ask the OS to open the login URL")
    args = parser.parse_args(argv)
    verifier, challenge, state = _pkce()
    url = account.build_login(verifier, challenge, state)
    stored = account._read_store()
    stored["pending"] = {"verifier": verifier, "state": state}
    account.write_store(stored)
    print("Open this URL and sign in. Finish any prompt the page shows.\n")
    print(url + "\n")
    if args.open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print("After signing in, the browser opens a kleinanzeigen.de address")
    print("(it may say it cannot open the app). Paste that FULL URL here.")
    redirect = input("URL: ").strip()
    signed_in = asyncio.run(account.complete_login(redirect, verifier, state))
    print(f"Signed in as {signed_in.email} (user {signed_in.user_id}).")
    print(f"Saved to {account.token_path()}")


if __name__ == "__main__":
    main()

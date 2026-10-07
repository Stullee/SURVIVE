#!/usr/bin/env python3
"""Pinterest Standard access demo: Ember's Pinterest flow, run against Pinterest's API sandbox.

An app with Trial access can't make pins at api.pinterest.com (Pinterest answers 403, "use API Sandbox instead"), so
Ember itself can't show its pin flow before Standard access is granted. This script makes the calls Ember's code makes
(ember/app/integrations/pinterest_live.py) against https://api-sandbox.pinterest.com, for the screen recording
Pinterest asks for with the upgrade request:

1. the OAuth consent screen on www.pinterest.com (the same app, scopes and redirect URI as Ember),
2. the code exchanged for a sandbox token,
3. the account read, a board made and a pin made on it, each with Pinterest's answer.

Run it on your own computer with the app's ID and secret at hand:

    python3 pinterest_sandbox_demo.py path/to/picture.png

It needs only Python 3.9 or newer, no packages. The secret is asked without showing it and no token is printed, so
the whole run can be filmed. Sandbox boards and pins are seen by you only and never move to your real account.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from getpass import getpass
from pathlib import Path
from typing import Any

AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
SANDBOX_URL = "https://api-sandbox.pinterest.com/v5"
SCOPES = ("boards:read", "boards:write", "pins:read", "pins:write", "user_accounts:read")  # Ember's own
REDIRECT_URI = "https://localhost/ember-pinterest"  # Ember's default
DISCLOSURE = "Designed with the help of AI and reviewed by the seller."  # the line Ember adds to every pin
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def call(
    method: str,
    path: str,
    *,
    token: str = "",
    basic: str = "",
    form: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One request to the sandbox, shown as it goes out and as Pinterest answers it; a refusal ends the run."""
    headers = {"Accept": "application/json"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif basic:
        headers["Authorization"] = f"Basic {basic}"
    print(f"\n> {method} {SANDBOX_URL}{path}")
    request = urllib.request.Request(  # noqa: S310 - always https://api-sandbox.pinterest.com
        SANDBOX_URL + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - as above
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except urllib.error.URLError as exc:
        sys.exit(f"Pinterest couldn't be reached: {exc.reason}")
    try:
        answer = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        answer = {}
    print(f"< HTTP {status}")
    if not 200 <= status < 300 or not isinstance(answer, dict):
        detail = answer.get("message") if isinstance(answer, dict) else None
        sys.exit(f"Pinterest refused: {detail or raw[:300].decode('utf-8', 'replace')}")
    return answer


def show(answer: dict[str, Any], *keys: str) -> None:
    print(json.dumps({key: answer.get(key) for key in keys}, indent=2, ensure_ascii=False))


def step(text: str) -> None:
    print(f"\n=== {text} ===")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 pinterest_sandbox_demo.py path/to/picture.png")
    picture = Path(sys.argv[1]).expanduser()
    content_type = MIME.get(picture.suffix.lower())
    if content_type is None or not picture.is_file():
        sys.exit(f"{picture} isn't a .png or .jpg file")

    step("Ember-AI: connect a Pinterest account (OAuth), then make a board and a pin (API sandbox)")
    app_id = input("Pinterest app ID: ").strip()
    secret = getpass("App secret (not shown): ").strip()
    redirect = input(f"Redirect URI [{REDIRECT_URI}]: ").strip() or REDIRECT_URI

    step("1. The owner allows access on Pinterest's consent screen")
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = (
        AUTHORIZE_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": app_id,
                "redirect_uri": redirect,
                "scope": ",".join(SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    print(f"Opening Pinterest in your browser (if it doesn't open, copy this address):\n{url}")
    webbrowser.open(url)
    print("\nAfter you allow access, the browser opens the redirect address. Its page doesn't load, as expected:")
    pasted = input("paste that whole address here: ").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query)
    if query.get("state", [""])[0] != state:
        sys.exit("That address belongs to another attempt to connect: run the script again")
    if "error" in query:
        sys.exit(f"Pinterest said: {query['error'][0][:100]}")
    code = query.get("code", [""])[0]
    if not code:
        sys.exit("The address has no code: allow access at Pinterest first")

    step("2. The code is exchanged for an access token")
    basic = base64.b64encode(f"{app_id}:{secret}".encode()).decode()
    tokens = call(
        "POST",
        "/oauth/token",
        basic=basic,
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "code_verifier": verifier,
        },
    )
    token = str(tokens.get("access_token") or "")
    if not token:
        sys.exit("Pinterest answered without a token")
    print(
        json.dumps(
            {
                "access_token": "(received, not shown)",
                "scope": tokens.get("scope"),
                "expires_in": tokens.get("expires_in"),
            },
            indent=2,
        )
    )

    step("3. Whose account it is")
    show(call("GET", "/user_account", token=token), "username", "account_type")

    step("4. Ember makes a board")
    board = call(
        "POST",
        "/boards",
        token=token,
        body={
            "name": f"Ember demo board {time.strftime('%Y-%m-%d %H:%M')}",
            "description": "Printable planners and templates from my Etsy shop.",
            "privacy": "PUBLIC",
        },
    )
    show(board, "id", "name", "privacy")

    step("5. Ember makes a pin on it")
    title = input("Pin title [Budget planner printable]: ").strip() or "Budget planner printable"
    link = input("Etsy listing address the pin links to (Enter for none): ").strip()
    description = "A printable monthly budget planner that keeps bills, savings and spending on one page."
    pin_body: dict[str, Any] = {
        "board_id": board["id"],
        "title": title,
        "description": f"{description}\n\n{DISCLOSURE}",
        "alt_text": title,
        "media_source": {
            "source_type": "image_base64",
            "content_type": content_type,
            "data": base64.b64encode(picture.read_bytes()).decode("ascii"),
        },
    }
    if link:
        pin_body["link"] = link
    pin = call("POST", "/pins", token=token, body=pin_body)
    show(pin, "id", "board_id", "title", "description", "link", "created_at")

    pin_url = f"https://www.pinterest.com/pin/{pin['id']}/"
    step("Done")
    print(f"The pin: {pin_url}\n(Sandbox pins are seen only by you, logged in to the account that allowed access.)")
    input("Press Enter to open it on pinterest.com... ")
    webbrowser.open(pin_url)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nStopped.")

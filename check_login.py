"""Check Fuelin login locally without printing credentials or changing workouts."""
import argparse
import base64
import binascii
import getpass
import http.cookiejar
import json
from datetime import datetime
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def diagnostic_summary(headers, body):
    """Return only fixed labels; never echo arbitrary headers or response text."""
    mime = headers.get("Content-Type", "").lower()
    content = body.lower()
    lines = ["Response format: " + ("JSON" if "json" in mime else "HTML" if "html" in mime else "other")]
    if "cloudflare" in headers.get("Server", "").lower() or headers.get("CF-Ray"):
        lines.append("Gateway: Cloudflare detected")
    for label, markers in {
        "Browser challenge": [b"cf-chl-", b"just a moment", b"enable javascript and cookies", b"captcha"],
        "CSRF / origin check": [b"csrf", b"invalid origin", b"origin not allowed"],
        "Access restriction": [b"access denied", b"forbidden", b"not authorized", b"not authorised"],
        "Credential error": [b"invalid credentials", b"wrong username or password"],
        "Rate limit": [b"too many requests", b"rate limit"],
    }.items():
        if any(marker in content for marker in markers):
            lines.append(label + " marker detected")
    lines.append("Raw response, cookies and tokens omitted.")
    return lines


def login_response_summary(headers, body):
    """Inspect known auth fields without exposing values, claims or unknown keys."""
    lines = diagnostic_summary(headers, body)
    for header in ("Authorization", "X-Auth-Token", "X-Access-Token", "Set-Cookie"):
        if headers.get(header):
            lines.append(f"Response header present: {header} (value hidden)")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        lines.append("JSON response: no")
        return lines
    lines.append("JSON response: yes")
    token_keys = {"token", "accessToken", "access_token", "refreshToken", "refresh_token",
                  "idToken", "id_token", "jwt", "sessionToken", "session_token"}
    container_keys = {"data", "result", "payload", "session", "auth", "authentication", "tokens", "user"}

    def inspect(value, path="response", depth=0):
        if depth > 5:
            return
        if isinstance(value, str):
            lines.append(f"String response at {path} (value hidden)")
            return
        if not isinstance(value, dict):
            return
        unknown = 0
        for key, item in value.items():
            if key in token_keys:
                present = isinstance(item, str) and bool(item)
                lines.append(f"Token field {path}.{key}: {'non-empty string' if present else 'empty or non-string'} (value hidden)")
            elif key in container_keys:
                inspect(item, path + "." + key, depth + 1)
            elif key in {"status", "message", "error", "success", "userId", "id", "tokenType", "token_type", "expiresIn", "expires_in"}:
                lines.append(f"Field present: {path}.{key} (value hidden)")
            else:
                unknown += 1
        if unknown:
            lines.append(f"Other fields at {path}: {unknown} (names and values hidden)")

    inspect(data)
    return lines


def token_user_id(token):
    """Read an ID hint from JWT claims, not a verified identity. Server checks follow."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        if not isinstance(claims, dict):
            return None
        nested = claims.get("user") if isinstance(claims.get("user"), dict) else {}
        candidates = [claims.get(key) for key in ("userId", "id", "sub")] + [nested.get("id"), nested.get("userId")]
        for candidate in candidates:
            try:
                if isinstance(candidate, str):
                    return str(UUID(candidate))
            except ValueError:
                continue
    except (ValueError, UnicodeDecodeError, binascii.Error):
        pass
    return None


def session_user_id(opener, token, email):
    """Use Fuelin's observed /auth base and Auth.js HTTPS session cookie convention."""
    request = Request("https://prod.fuelin.io/core/auth/session", headers={
        "Cookie": "__Secure-authjs.session-token=" + token,
        "Accept": "application/json", "User-Agent": "TrainerRoad-Fuelin-Personal-Sync/0.1"})
    try:
        with opener.open(request, timeout=30) as response:
            session = json.loads(response.read(1_000_000))
        user = session.get("user") if isinstance(session, dict) else None
        if not isinstance(user, dict) or not isinstance(user.get("email"), str) or user["email"].casefold() != email.casefold():
            print("Session identity check: no matching account returned (body hidden).")
            return None
        for candidate in (user.get("id"), user.get("userId"), session.get("userId")):
            try:
                if isinstance(candidate, str):
                    uid = str(UUID(candidate))
                    print("Session identity check: matching account UUID retrieved (value hidden).")
                    return uid
            except ValueError:
                continue
        print("Session identity check: matching email, but no account UUID returned.")
    except HTTPError as error:
        print(f"Session identity check returned HTTP {error.code}.")
    except (ValueError, UnicodeDecodeError):
        print("Session identity check: unrecognised response (body hidden).")
    return None


def verify_bearer(opener, token, email, user_id=None):
    """Read only the matching account and today's workouts; never print account data."""
    transport = "Bearer"

    def read(path):
        nonlocal transport
        headers = {"Accept": "application/json", "User-Agent": "TrainerRoad-Fuelin-Personal-Sync/0.1"}
        headers.update({"Authorization": "Bearer " + token} if transport == "Bearer" else
                       {"Cookie": "__Secure-authjs.session-token=" + token})
        request = Request("https://prod.fuelin.io/core" + path,
                          headers=headers)
        try:
            with opener.open(request, timeout=30) as response:
                return json.loads(response.read(2_000_000))
        except HTTPError as error:
            if error.code not in (401, 403) or transport != "Bearer":
                raise
            error.close()
            print("Bearer request refused; checking Auth.js session-cookie transport on the same read endpoint.")
            transport = "session cookie"
            return read(path)

    try:
        user_id = user_id or token_user_id(token) or session_user_id(opener, token, email)
        if not user_id:
            print("No readable account ID in token (it may be opaque or encrypted).")
            print("Direct profile check needs your own Fuelin account UUID via --user-id; no user-list request was made.")
            return False
        user_id = str(UUID(user_id))
        user = read("/api/v1/users/" + quote(user_id, safe=""))
        if not isinstance(user, dict) or not isinstance(user.get("email"), str) or user["email"].casefold() != email.casefold() or user.get("id") != user_id:
            print("Bearer profile check: response did not match your account (body hidden).")
            return False
        print("Bearer profile check: matching account retrieved.")
        zone = ZoneInfo(user.get("timezone") or "UTC")
        day = datetime.now(zone).date().isoformat()
        workouts = read(f"/api/v1/users/{quote(user['id'], safe='')}/days/{day}/workouts")
        if not isinstance(workouts, list):
            print("Workout read check: unexpected response shape (body hidden).")
            return False
        print(f"Workout read check: succeeded ({len(workouts)} workouts today).")
        print(f"Authenticated reads succeeded using {transport}. No workouts were changed.")
        return True
    except HTTPError as error:
        print(f"Bearer read check returned HTTP {error.code}.")
        for line in diagnostic_summary(error.headers, error.read(65536)):
            print(line)
    except (ValueError, KeyError):
        print("Bearer read check: response or account timezone was not recognised (body hidden).")
    return False


def main():
    # Import here: the shared client uses the diagnostic helpers above.
    from auth_store import StorageError
    from create_workout import FuelinClient, SyncError
    parser = argparse.ArgumentParser(description="Check Fuelin access, reusing a Keychain session when available.")
    parser.add_argument("--refresh", action="store_true", help="Sign in again and replace the saved token")
    args = parser.parse_args()
    try:
        client = FuelinClient()
        user = client.login(refresh=args.refresh)
        zone = ZoneInfo(user.get("timezone") or "UTC")
        day = datetime.now(zone).date().isoformat()
        workouts = client.request("GET", f"/api/v1/users/{client.user_id}/days/{day}/workouts")
        if not isinstance(workouts, list):
            raise SyncError("Workout read returned an unexpected response")
        print(f"Workout read check: succeeded ({len(workouts)} workouts today).")
        print("No workouts were changed. Password was not saved.")
        return 0
    except (StorageError, SyncError) as error:
        print(str(error))
    except HTTPError as error:
        print(f"Authentication/read request returned HTTP {error.code}.")
        for line in diagnostic_summary(error.headers, error.read(65536)):
            print(line)
    except Exception:
        print("Authentication/read request failed. Credentials and tokens were not printed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

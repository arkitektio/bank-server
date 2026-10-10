"""A fake Enable Banking API for the bank service's test suite.

Implements the slice of https://api.enablebanking.com the service uses — ``/aspsps``,
``/auth``, ``/sessions``, ``/accounts/{uid}/transactions`` (paged by ``continuation_key``)
and ``/accounts/{uid}/balances`` — and verifies every request's RS256 application JWT the way
the real API does. Tests drive it through ``/_admin``:

* ``POST /_admin/keys``            register an application's public key (``kid``, ``public_key``)
* ``POST /_admin/scenario``        the accounts a bank (``aspsp_name``) offers, with their data
* ``PUT  /_admin/accounts/{ident}`` replace one account's transactions / balances / ``rate_limited``
* ``POST /_admin/approve``         the user approves a consent: returns the redirect ``code``
* ``POST /_admin/sessions/{id}/expire``  the consent runs out at the bank
* ``POST /_admin/hold`` / ``/_admin/release``  park transaction and session requests until released
* ``GET  /_admin/held``            how many requests are parked right now
* ``GET  /_admin/log``             every API request with its PSU headers

Accounts get a fresh ``uid`` per session, as at Enable Banking; the ``identification_hash``
stays the same across sessions.
"""

import asyncio
import uuid

import jwt
from aiohttp import web

PAGE_SIZE = 3

STATE: dict = {
    "keys": {},  # kid -> public key PEM
    "redirects": {},  # kid -> redirect URLs registered for the application
    "scenarios": {},  # aspsp name -> [account ident]
    "accounts": {},  # ident -> {"account": {...}, "transactions": [...], "balances": [...]}
    "auths": {},  # state -> {aspsp, redirect_url, valid_until}
    "codes": {},  # code -> state
    "sessions": {},  # session_id -> {status, uids: {uid: ident}, valid_until}
    "uids": {},  # uid -> session_id
    "log": [],
    "hold": asyncio.Event(),
    "held": 0,
}
STATE["hold"].set()


def _error(status: int, code: str, message: str = "") -> web.Response:
    return web.json_response({"error": code, "message": message or code}, status=status)


@web.middleware
async def verify_jwt(request: web.Request, handler):  # noqa: ANN001, ANN201
    if request.path.startswith("/_admin"):
        return await handler(request)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return _error(401, "UNAUTHORIZED", "missing bearer token")
    token = auth.removeprefix("Bearer ")
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        key = STATE["keys"].get(kid)
        if key is None:
            return _error(401, "UNAUTHORIZED", f"unknown application {kid!r}")
        jwt.decode(token, key, algorithms=["RS256"], audience="api.enablebanking.com", issuer="enablebanking.com")
        request["kid"] = kid
    except jwt.PyJWTError as error:
        return _error(401, "UNAUTHORIZED", f"invalid JWT: {error}")
    STATE["log"].append(
        {
            "method": request.method,
            "path": request.path,
            "kid": kid,
            "query": dict(request.query),
            "psu_ip": request.headers.get("Psu-Ip-Address"),
            "psu_user_agent": request.headers.get("Psu-User-Agent"),
        }
    )
    return await handler(request)


# --- the API ----------------------------------------------------------------------------------


async def application(request: web.Request) -> web.Response:
    kid = request["kid"]
    return web.json_response(
        {"name": f"Fake application {kid}", "description": "", "kid": kid, "environment": "SANDBOX", "redirect_urls": STATE["redirects"].get(kid, []), "active": True, "countries": ["AT"], "services": ["AIS"]}
    )


async def aspsps(request: web.Request) -> web.Response:
    country = request.query.get("country")
    banks = [{"name": name, "country": "AT", "bic": "FAKEATWW", "logo": None, "maximum_consent_validity": 180 * 86400} for name in STATE["scenarios"]]
    return web.json_response({"aspsps": [b for b in banks if not country or b["country"] == country]})


async def auth(request: web.Request) -> web.Response:
    body = await request.json()
    name = body["aspsp"]["name"]
    if name not in STATE["scenarios"]:
        return _error(422, "ASPSP_NOT_FOUND", f"unknown bank {name!r}")
    STATE["auths"][body["state"]] = {"aspsp": name, "redirect_url": body["redirect_url"], "valid_until": body["access"]["valid_until"]}
    return web.json_response({"url": f"https://fakebank.test/login?state={body['state']}", "authorization_id": uuid.uuid4().hex})


async def create_session(request: web.Request) -> web.Response:
    body = await request.json()
    if not STATE["hold"].is_set():
        STATE["held"] += 1
        try:
            await STATE["hold"].wait()
        finally:
            STATE["held"] -= 1
    state = STATE["codes"].pop(body.get("code"), None)
    if state is None:
        return _error(422, "INVALID_CODE", "unknown or already used code")
    pending = STATE["auths"][state]
    session_id = uuid.uuid4().hex
    uids, accounts = {}, []
    for ident in STATE["scenarios"][pending["aspsp"]]:
        uid = uuid.uuid4().hex
        uids[uid] = ident
        STATE["uids"][uid] = session_id
        accounts.append({**STATE["accounts"][ident]["account"], "uid": uid, "identification_hash": ident})
    STATE["sessions"][session_id] = {"status": "AUTHORIZED", "uids": uids, "valid_until": pending["valid_until"]}
    return web.json_response(
        {
            "session_id": session_id,
            "accounts": accounts,
            "aspsp": {"name": pending["aspsp"], "country": "AT"},
            "psu_type": "personal",
            "access": {"valid_until": pending["valid_until"]},
        }
    )


async def get_session(request: web.Request) -> web.Response:
    session = STATE["sessions"].get(request.match_info["id"])
    if session is None:
        return _error(404, "SESSION_DOES_NOT_EXIST")
    return web.json_response({"status": session["status"], "accounts": list(session["uids"]), "access": {"valid_until": session["valid_until"]}})


async def delete_session(request: web.Request) -> web.Response:
    session = STATE["sessions"].get(request.match_info["id"])
    if session is None:
        return _error(404, "SESSION_DOES_NOT_EXIST")
    session["status"] = "REVOKED"
    return web.json_response({"message": "OK"})


def _account(request: web.Request) -> tuple[dict | None, web.Response | None]:
    uid = request.match_info["uid"]
    session = STATE["sessions"].get(STATE["uids"].get(uid, ""))
    if session is None:
        return None, _error(404, "ACCOUNT_DOES_NOT_EXIST")
    if session["status"] != "AUTHORIZED":
        return None, _error(401, "EXPIRED_SESSION", f"session is {session['status']}")
    return STATE["accounts"][session["uids"][uid]], None


async def transactions(request: web.Request) -> web.Response:
    account, error = _account(request)
    if error:
        return error
    if not STATE["hold"].is_set():
        STATE["held"] += 1
        try:
            await STATE["hold"].wait()
        finally:
            STATE["held"] -= 1
    if account.get("rate_limited"):
        # The bank's PSD2 access limit for today is used up (no Retry-After, as banks send it).
        return _error(429, "ASPSP_RATE_LIMIT_EXCEEDED", "too many requests for this account today")
    rows = account["transactions"]
    if "date_from" in request.query:
        since = request.query["date_from"]
        rows = [t for t in rows if (t.get("booking_date") or t.get("transaction_date") or "") >= since]
    start = int(request.query.get("continuation_key") or 0)
    page = rows[start : start + PAGE_SIZE]
    more = start + PAGE_SIZE < len(rows)
    return web.json_response({"transactions": page, "continuation_key": str(start + PAGE_SIZE) if more else None})


async def balances(request: web.Request) -> web.Response:
    account, error = _account(request)
    if error:
        return error
    return web.json_response({"balances": account["balances"]})


# --- admin ------------------------------------------------------------------------------------


async def admin_keys(request: web.Request) -> web.Response:
    body = await request.json()
    STATE["keys"][body["kid"]] = body["public_key"]
    STATE["redirects"][body["kid"]] = body.get("redirect_urls", [])
    return web.json_response({"ok": True})


async def admin_scenario(request: web.Request) -> web.Response:
    body = await request.json()
    idents = []
    for account in body["accounts"]:
        ident = account["identification_hash"]
        STATE["accounts"][ident] = {
            "account": {k: v for k, v in account.items() if k not in ("transactions", "balances")},
            "transactions": account.get("transactions", []),
            "balances": account.get("balances", []),
        }
        idents.append(ident)
    STATE["scenarios"][body["aspsp_name"]] = idents
    return web.json_response({"ok": True})


async def admin_account(request: web.Request) -> web.Response:
    body = await request.json()
    entry = STATE["accounts"][request.match_info["ident"]]
    for key in ("transactions", "balances", "rate_limited"):
        if key in body:
            entry[key] = body[key]
    return web.json_response({"ok": True})


async def admin_approve(request: web.Request) -> web.Response:
    body = await request.json()
    if body["state"] not in STATE["auths"]:
        return _error(404, "UNKNOWN_STATE")
    code = uuid.uuid4().hex
    STATE["codes"][code] = body["state"]
    return web.json_response({"code": code})


async def admin_expire(request: web.Request) -> web.Response:
    STATE["sessions"][request.match_info["id"]]["status"] = "EXPIRED"
    return web.json_response({"ok": True})


async def admin_hold(request: web.Request) -> web.Response:
    STATE["hold"].clear()
    return web.json_response({"ok": True})


async def admin_release(request: web.Request) -> web.Response:
    STATE["hold"].set()
    return web.json_response({"ok": True})


async def admin_held(request: web.Request) -> web.Response:
    return web.json_response({"held": STATE["held"]})


async def admin_log(request: web.Request) -> web.Response:
    return web.json_response({"log": STATE["log"]})


def build() -> web.Application:
    app = web.Application(middlewares=[verify_jwt])
    app.add_routes(
        [
            web.get("/application", application),
            web.get("/aspsps", aspsps),
            web.post("/auth", auth),
            web.post("/sessions", create_session),
            web.get("/sessions/{id}", get_session),
            web.delete("/sessions/{id}", delete_session),
            web.get("/accounts/{uid}/transactions", transactions),
            web.get("/accounts/{uid}/balances", balances),
            web.post("/_admin/keys", admin_keys),
            web.post("/_admin/scenario", admin_scenario),
            web.put("/_admin/accounts/{ident}", admin_account),
            web.post("/_admin/approve", admin_approve),
            web.post("/_admin/sessions/{id}/expire", admin_expire),
            web.post("/_admin/hold", admin_hold),
            web.post("/_admin/release", admin_release),
            web.get("/_admin/held", admin_held),
            web.get("/_admin/log", admin_log),
            web.get("/_admin/health", lambda request: web.json_response({"ok": True})),
        ]
    )
    return app


if __name__ == "__main__":
    web.run_app(build(), host="0.0.0.0", port=8000, print=None)

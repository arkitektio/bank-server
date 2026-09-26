"""A fake Scalable Capital (OAuth issuer + CLI GraphQL API) for the bank service's test suite.

Implements what the service uses of ``secure.scalable.capital`` and ``/api/cli/graphql``, with
the protocol behaviour that matters:

* **DPoP** on every request: the proof's ES256 signature, ``htm``/``htu``, ``jti`` replay, and —
  on GraphQL — ``ath`` and the access token's ``cnf.jkt`` binding. The token endpoint answers a
  new key with a ``use_dpop_nonce`` challenge first, as the real issuer does.
* **Device authorization** (pending → ``slow_down`` if polled too fast → approved) and
  **rotating refresh tokens**: each refresh kills the used token, and presenting a used token
  again revokes the whole login (reuse detection).
* **GraphQL** answers only the operations the service may send, from per-person data seeded by
  the tests; anything else is a 400 and shows up in the log.

Tests drive it through ``/_admin``:

* ``POST /_admin/people/{person}``   seed a person: portfolios, holdings, transactions, savings, mfa
* ``POST /_admin/approve``           the user approves a device code (``user_code``, ``person``)
* ``POST /_admin/deny``              the user denies it
* ``POST /_admin/mfa``               set the 2FA outcome for a person (``status``)
* ``POST /_admin/revoke``            revoke every login of a person (as a password change would)
* ``POST /_admin/config``            ``token_ttl``, ``interval``
* ``POST /_admin/hold`` / ``release`` park refresh requests until released
* ``GET  /_admin/held``, ``/_admin/log``, ``/_admin/health``
"""

import asyncio
import base64
import hashlib
import json
import secrets
import time
import uuid

import jwt
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import rsa

SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
NS = "https://de.scalable.capital/"
ALLOWED = {
    "ResolveBrokerIds",
    "Is2faOnLoginEnabled",
    "Start2faOnLogin",
    "Validate2faOnLogin",
    "BrokerOverview",
    "BrokerHoldings",
    "BrokerLimits",
    "BrokerTransactions",
    "DiscoverOvernightAccounts",
    "OvernightSummary",
    "OvernightTransactions",
    "BrokerChart",
    "BrokerQuote",
}

STATE: dict = {
    "config": {"token_ttl": 1200, "interval": 5, "graphql_nonce_challenges": 0, "graphql_rejections": 0, "graphql_rate_limited": 0},
    "people": {},  # person -> seeded data
    "prices": {},  # isin -> [[iso day, mid price], ...] (market data for BrokerChart / BrokerQuote)
    "devices": {},  # device_code -> {user_code, jkt, person, denied, last_poll, expires}
    "families": {},  # family -> {person, jkt, current, used: set, revoked, session}
    "refresh": {},  # refresh token -> family
    "nonces": {},  # jkt -> nonce handed out
    "jtis": set(),
    "mfa": {},  # mfa session -> person
    "log": [],
    "hold": asyncio.Event(),
    "held": 0,
}
STATE["hold"].set()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _thumbprint(jwk: dict) -> str:
    canonical = json.dumps({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]}, separators=(",", ":"))
    return _b64(hashlib.sha256(canonical.encode()).digest())


def _oauth_error(status: int, error: str, headers: dict | None = None) -> web.Response:
    return web.json_response({"error": error, "error_description": error}, status=status, headers=headers)


def _verify_proof(request: web.Request, access_token: str | None = None) -> str:
    """The proof's key thumbprint; raises ValueError(error code) if the proof is not acceptable."""
    proof = request.headers.get("DPoP")
    if not proof:
        raise ValueError("invalid_dpop_proof")
    header = jwt.get_unverified_header(proof)
    if header.get("typ") != "dpop+jwt" or header.get("alg") != "ES256" or "jwk" not in header:
        raise ValueError("invalid_dpop_proof")
    key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(header["jwk"]))
    claims = jwt.decode(proof, key, algorithms=["ES256"])
    htu = f"{request.scheme}://{request.host}{request.path}"
    if claims.get("htm") != request.method or claims.get("htu") != htu:
        raise ValueError("invalid_dpop_proof")
    if abs(time.time() - claims.get("iat", 0)) > 60 or claims.get("jti") in STATE["jtis"]:
        raise ValueError("invalid_dpop_proof")
    STATE["jtis"].add(claims["jti"])
    if access_token is not None and claims.get("ath") != _b64(hashlib.sha256(access_token.encode()).digest()):
        raise ValueError("invalid_dpop_proof")
    jkt = _thumbprint(header["jwk"])
    request["dpop_nonce"] = claims.get("nonce")
    return jkt


def _log(request: web.Request, **extra) -> None:
    STATE["log"].append({"path": request.path, **extra})


def _issue(family_id: str) -> dict:
    family = STATE["families"][family_id]
    now = int(time.time())
    ttl = STATE["config"]["token_ttl"]
    access = jwt.encode(
        {
            "iss": "fakescalable",
            "sub": f"auth0|{family['person']}",
            "aud": ["https://de.scalable.capital/api-gateway"],
            "iat": now,
            "exp": now + ttl,
            "cnf": {"jkt": family["jkt"]},
            NS + "person_id": family["person"],
            NS + "session_id": family["session"],
        },
        SIGNING_KEY,
        algorithm="RS256",
    )
    refresh = secrets.token_urlsafe(24)
    if family["current"]:
        family["used"].add(family["current"])
    family["current"] = refresh
    STATE["refresh"][refresh] = family_id
    return {"access_token": access, "refresh_token": refresh, "expires_in": ttl, "token_type": "DPoP", "scope": "offline_access openid email"}


async def device_code(request: web.Request) -> web.Response:
    try:
        jkt = _verify_proof(request)
    except Exception:
        return _oauth_error(400, "invalid_dpop_proof")
    form = await request.post()
    _log(request, grant="device_code_start", jkt=jkt)
    code, user_code = secrets.token_urlsafe(24), f"{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
    STATE["devices"][code] = {"user_code": user_code, "jkt": jkt, "person": None, "denied": False, "last_poll": 0.0, "used": False, "client_id": form.get("client_id")}
    base = f"{request.scheme}://{request.host}/activate"
    return web.json_response(
        {
            "device_code": code,
            "user_code": user_code,
            "verification_uri": base,
            "verification_uri_complete": f"{base}?user_code={user_code}",
            "expires_in": 900,
            "interval": STATE["config"]["interval"],
        }
    )


async def token(request: web.Request) -> web.Response:
    try:
        jkt = _verify_proof(request)
    except Exception:
        return _oauth_error(400, "invalid_dpop_proof")
    nonce = STATE["nonces"].setdefault(jkt, secrets.token_urlsafe(12))
    if request["dpop_nonce"] != nonce:
        return _oauth_error(400, "use_dpop_nonce", headers={"DPoP-Nonce": nonce})
    form = await request.post()
    grant = form.get("grant_type")
    _log(request, grant=grant, jkt=jkt)

    if grant == "urn:ietf:params:oauth:grant-type:device_code":
        device = STATE["devices"].get(form.get("device_code"))
        if device is None or device["used"]:
            return _oauth_error(400, "invalid_grant")
        if device["jkt"] != jkt:
            return _oauth_error(400, "invalid_dpop_proof")
        if device["denied"]:
            return _oauth_error(403, "access_denied")
        if device["person"] is None:
            too_fast = time.time() - device["last_poll"] < STATE["config"]["interval"] - 0.5
            device["last_poll"] = time.time()
            return _oauth_error(400, "slow_down" if too_fast else "authorization_pending")
        device["used"] = True
        family = uuid.uuid4().hex
        STATE["families"][family] = {"person": device["person"], "jkt": jkt, "current": None, "used": set(), "revoked": False, "session": uuid.uuid4().hex, "refreshes": 0}
        return web.json_response(_issue(family))

    if grant == "refresh_token":
        STATE["held"] += 1
        try:
            await STATE["hold"].wait()
        finally:
            STATE["held"] -= 1
        presented = form.get("refresh_token")
        family_id = STATE["refresh"].get(presented)
        family = STATE["families"].get(family_id) if family_id else None
        if family is None or family["revoked"] or family["jkt"] != jkt:
            return _oauth_error(400, "invalid_grant")
        if presented != family["current"]:
            family["revoked"] = True  # reuse detection: a rotated-away token came back
            return _oauth_error(400, "invalid_grant")
        if form.get("session_id") != family["session"]:
            return _oauth_error(400, "invalid_request")
        family["refreshes"] += 1
        return web.json_response(_issue(family_id))

    return _oauth_error(400, "unsupported_grant_type")


async def revoke(request: web.Request) -> web.Response:
    try:
        _verify_proof(request)
    except Exception:
        return _oauth_error(400, "invalid_dpop_proof")
    form = await request.post()
    family_id = STATE["refresh"].get(form.get("token"))
    if family_id:
        STATE["families"][family_id]["revoked"] = True
    _log(request, grant="revoke", family=family_id)
    return web.Response(status=200)


def _page(rows: list[dict], page_input: dict) -> dict:
    size = int(page_input.get("pageSize") or 20)
    start = int(page_input.get("cursor") or 0)
    chunk = rows[start : start + size]
    nxt = start + size
    return {"cursor": str(nxt) if nxt < len(rows) else None, "total": len(chunk), "transactions": chunk}


def _answer(person: dict, person_id: str, operation: str, variables: dict) -> dict | None:
    pid = variables.get("portfolioId")
    if pid is not None and pid not in person["portfolios"]:
        return None
    portfolio = person["portfolios"].get(pid) or {}
    if operation == "ResolveBrokerIds":
        return {"account": {"id": person_id, "brokerPortfolios": [{"id": p} for p in person["portfolios"]]}}
    if operation == "Is2faOnLoginEnabled":
        return {"is2faOnLoginEnabled": {"enabled": person.get("mfa", False), "hasApprovedSession": False}}
    if operation == "Start2faOnLogin":
        session = uuid.uuid4().hex
        STATE["mfa"][session] = person_id
        return {"start2faOnLogin": {"mfaSessionId": session}}
    if operation == "Validate2faOnLogin":
        if STATE["mfa"].get(variables["input"]["mfaSessionId"]) != person_id:
            return None
        return {"validate2faOnLogin": {"status": person.get("mfa_status", "PENDING")}}
    if operation == "BrokerOverview":
        securities = sum(h["portfolioIsinPerformance"]["valuation"] for h in portfolio.get("holdings", []))
        crypto = portfolio.get("crypto", 0)
        return {"account": {"brokerPortfolio": {"valuation": {"valuation": securities + crypto + portfolio.get("cash", 0), "securitiesValuation": securities, "cryptoValuation": crypto, "timestampUtc": {"time": "2026-09-25T21:00:00.000Z"}}}}}
    if operation == "BrokerQuote":
        prices = STATE["prices"].get(variables["isin"])
        if not prices:
            return None
        day, price = prices[-1]
        return {"account": {"brokerPortfolio": {"security": {"isin": variables["isin"], "name": f"Fund {variables['isin']}", "quoteTick": {
            "midPrice": price, "bidPrice": round(price - 0.05, 4), "askPrice": round(price + 0.05, 4), "currency": "EUR", "isOutdated": False, "timestampUtc": {"time": f"{day}T16:00:00.000Z"}}}}}}
    if operation == "BrokerHoldings":
        return {"account": {"brokerPortfolio": {"inventory": {"items": portfolio.get("holdings", [])}}}}
    if operation == "BrokerLimits":
        cash = portfolio.get("cash", 0)
        return {"account": {"brokerPortfolio": {"payments": {"buyingPower": {"cashBalance": cash, "cashAvailableToInvest": cash}, "withdrawalPower": {"cashAvailableForWithdrawal": cash}}}}}
    if operation == "BrokerTransactions":
        return {"account": {"brokerPortfolio": {"moreTransactions": _page(portfolio.get("transactions", []), variables["input"])}}}
    if operation == "DiscoverOvernightAccounts":
        return {"account": {"savingsAccounts": [{"__typename": "OvernightSavingsAccount", "id": sid, "state": s.get("state", "ACTIVE"), "personalizations": {"name": s.get("name", "Tagesgeld")}} for sid, s in person.get("savings", {}).items()]}}
    savings = person.get("savings", {}).get(variables.get("savingsAccountId"))
    if operation in ("OvernightSummary", "OvernightTransactions") and savings is None:
        return None
    if operation == "OvernightSummary":
        return {"account": {"savingsAccount": {"id": variables["savingsAccountId"], "totalAmount": savings.get("total", 0)}}}
    if operation == "OvernightTransactions":
        return {"account": {"savingsAccount": {"id": variables["savingsAccountId"], "moreTransactions": _page(savings.get("transactions", []), variables["input"])}}}
    return None


async def graphql(request: web.Request) -> web.Response:
    body = await request.json()
    operation = body.get("operationName")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("DPoP "):
        _log(request, operation=operation, rejected="no_token")
        return web.json_response({"errors": [{"message": "unauthorized"}]}, status=401)
    access = auth.removeprefix("DPoP ")
    try:
        claims = jwt.decode(access, SIGNING_KEY.public_key(), algorithms=["RS256"], audience="https://de.scalable.capital/api-gateway")
        jkt = _verify_proof(request, access_token=access)
    except Exception as error:
        _log(request, operation=operation, rejected=f"auth:{type(error).__name__}")
        return web.json_response({"errors": [{"message": "unauthorized"}]}, status=401)
    if claims["cnf"]["jkt"] != jkt:
        _log(request, operation=operation, rejected="jkt")
        return web.json_response({"errors": [{"message": "unauthorized"}]}, status=401)
    config = STATE["config"]
    if config["graphql_nonce_challenges"] > 0 and request["dpop_nonce"] is None:
        # RFC 9449 resource-server style: the error is in WWW-Authenticate, not the body.
        config["graphql_nonce_challenges"] -= 1
        _log(request, operation=operation, rejected="use_dpop_nonce")
        return web.json_response(
            {"errors": [{"message": "unauthorized"}]},
            status=401,
            headers={"DPoP-Nonce": secrets.token_urlsafe(12), "WWW-Authenticate": 'DPoP error="use_dpop_nonce"'},
        )
    if config["graphql_rate_limited"] > 0:
        config["graphql_rate_limited"] -= 1
        _log(request, operation=operation, rejected="rate_limited")
        return web.json_response({"errors": [{"message": "too many requests"}]}, status=429, headers={"Retry-After": "120"})
    if config["graphql_rejections"] > 0:
        config["graphql_rejections"] -= 1
        _log(request, operation=operation, rejected="token")
        return web.json_response({"errors": [{"message": "unauthorized"}]}, status=401)
    _log(request, operation=operation, variables=body.get("variables"))
    if operation not in ALLOWED:
        return web.json_response({"errors": [{"message": f"operation {operation} not allowed"}]}, status=400)
    person_id = claims[NS + "person_id"]
    variables = body.get("variables") or {}
    account_id = variables.get("accountId") or variables.get("id") or (variables.get("input") or {}).get("userId")
    if operation == "BrokerChart":  # market data: not tied to the account
        prices = STATE["prices"].get(variables["isin"], [])
        points = [{"midPrice": price, "timestampUtc": {"time": f"{day}T16:00:00.000Z"}} for day, price in prices]
        return web.json_response({"data": {"timeSeriesBySecurity": [{"isin": variables["isin"], "timeFrame": variables["timeFrames"][0], "currency": "EUR", "dataPoints": points}]}})
    if account_id != person_id:
        return web.json_response({"data": None, "errors": [{"message": "forbidden"}]})
    data = _answer(STATE["people"].get(person_id, {"portfolios": {}}), person_id, operation, variables)
    if data is None:
        return web.json_response({"data": None, "errors": [{"message": "not found"}]})
    return web.json_response({"data": data})


async def admin_prices(request: web.Request) -> web.Response:
    STATE["prices"].update(await request.json())
    return web.json_response({"ok": True})


async def admin_person(request: web.Request) -> web.Response:
    STATE["people"][request.match_info["person"]] = await request.json()
    return web.json_response({"ok": True})


async def admin_approve(request: web.Request) -> web.Response:
    body = await request.json()
    for device in STATE["devices"].values():
        if device["user_code"] == body["user_code"]:
            device["person"] = body["person"]
            return web.json_response({"ok": True})
    return web.json_response({"error": "unknown user_code"}, status=404)


async def admin_deny(request: web.Request) -> web.Response:
    body = await request.json()
    for device in STATE["devices"].values():
        if device["user_code"] == body["user_code"]:
            device["denied"] = True
    return web.json_response({"ok": True})


async def admin_mfa(request: web.Request) -> web.Response:
    body = await request.json()
    STATE["people"].setdefault(body["person"], {"portfolios": {}})["mfa_status"] = body["status"]
    return web.json_response({"ok": True})


async def admin_revoke(request: web.Request) -> web.Response:
    body = await request.json()
    for family in STATE["families"].values():
        if family["person"] == body["person"]:
            family["revoked"] = True
    return web.json_response({"ok": True})


async def admin_families(request: web.Request) -> web.Response:
    person = request.query["person"]
    return web.json_response(
        {"families": [{"revoked": f["revoked"], "refreshes": f["refreshes"]} for f in STATE["families"].values() if f["person"] == person]}
    )


async def admin_config(request: web.Request) -> web.Response:
    STATE["config"].update(await request.json())
    return web.json_response(STATE["config"])


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
    app = web.Application()
    app.add_routes(
        [
            web.post("/oauth/device/code", device_code),
            web.post("/oauth/token", token),
            web.post("/oauth/revoke", revoke),
            web.post("/api/cli/graphql", graphql),
            web.post("/_admin/people/{person}", admin_person),
            web.post("/_admin/prices", admin_prices),
            web.post("/_admin/approve", admin_approve),
            web.post("/_admin/deny", admin_deny),
            web.post("/_admin/mfa", admin_mfa),
            web.post("/_admin/revoke", admin_revoke),
            web.get("/_admin/families", admin_families),
            web.post("/_admin/config", admin_config),
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

"""Throwaway spike: does the official Scalable CLI's OAuth flow work from Python?

    .venv/bin/python scripts/scalable_spike.py login     # device login (+2FA), queries, one refresh
    .venv/bin/python scripts/scalable_spike.py refresh   # reuse the saved session: refresh + queries

Everything it learns lands in ``.scalable-spike/`` (gitignored, 0600): the session (tokens + DPoP
key, so ``refresh`` can be re-run hours later to measure refresh-token lifetime), each raw
GraphQL response, and ``report.json``. It only sends read queries and the three 2FA-on-login
operations; it never trades.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
import jwt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from finance.scalable.dpop import DpopKey  # noqa: E402

ISSUER = "https://secure.scalable.capital"
AUDIENCE = "https://de.scalable.capital/api-gateway"
CLIENT_ID = "yBM3BrpRgwSTJZRdJllvtD6jJEmyxWfE"
SCOPE = "offline_access openid email"
GRAPHQL = "https://de.scalable.capital/api/cli/graphql"
USER_AGENT = os.environ.get("SPIKE_UA", "arkitekt-bank/0.1 (+scalable-spike)")

OUT = Path(__file__).resolve().parents[1] / ".scalable-spike"
SESSION = OUT / "session.json"
REPORT = OUT / "report.json"

Q_RESOLVE = "query ResolveBrokerIds($id: ID!) { account(id: $id) { id brokerPortfolios { id } } }"
Q_2FA_STATE = """query Is2faOnLoginEnabled($input: Is2faOnLoginEnabledInput!) {
  is2faOnLoginEnabled(input: $input) { enabled hasApprovedSession } }"""
M_2FA_START = """mutation Start2faOnLogin($input: Start2faOnLoginInput!) {
  start2faOnLogin(input: $input) { mfaSessionId } }"""
M_2FA_VALIDATE = """mutation Validate2faOnLogin($input: Validate2faOnLoginInput!) {
  validate2faOnLogin(input: $input) { status } }"""
Q_OVERVIEW = """query BrokerOverview($accountId: ID!, $portfolioId: ID!, $includeYearToDate: Boolean!) {
  account(id: $accountId) { brokerPortfolio(id: $portfolioId) {
    valuation(includeYearToDate: $includeYearToDate) {
      valuation securitiesValuation cryptoValuation
      timestampUtc { time } lastInventoryUpdateTimestampUtc { time }
      timeWeightedReturnByTimeframe { timeframe simpleAbsoluteReturn } } } } }"""
Q_HOLDINGS = """query BrokerHoldings($accountId: ID!, $portfolioId: ID!, $includeYearToDate: Boolean!, $quoteSource: MarketDataSource) {
  account(id: $accountId) { brokerPortfolio(id: $portfolioId) { inventory { items {
    isin name type
    inventory { position { filled pending blocked fifoPrice } }
    portfolioIsinPerformance { valuation currency }
    quoteTick(source: $quoteSource, includeYearToDate: $includeYearToDate) {
      midPrice currency timestampUtc { time } isOutdated } } } } } }"""
Q_LIMITS = """query BrokerLimits($accountId: ID!, $portfolioId: ID!) {
  account(id: $accountId) { brokerPortfolio(id: $portfolioId) { payments {
    buyingPower { cashBalance liveLimit loaned pendingBuyOrdersAmount pendingWithdrawalsAmount
      pendingSavingsPlanAmount pendingDividendsReinvestmentAmount pendingPocketMoneyAmount
      estimatedTaxes directDebit cashAvailableToInvest cashAvailableToInvestWithoutCredit }
    withdrawalPower { cashAvailableToInvest sellTradesAmount withdrawalDirectDebit cashAvailableForWithdrawal } } } } }"""
Q_TRANSACTIONS = """query BrokerTransactions($accountId: ID!, $portfolioId: ID!, $input: BrokerTransactionInput!) {
  account(id: $accountId) { brokerPortfolio(id: $portfolioId) { moreTransactions(input: $input) {
    cursor total
    transactions {
      __typename id currency type status isCancellation lastEventDateTime description custodian
      ... on BrokerSecurityTransactionSummary { isin securityTransactionType quantity amount side limitPrice stopPrice }
      ... on BrokerCashTransactionSummary { relatedIsin cashTransactionType amount }
      ... on BrokerNonTradeSecurityTransactionSummary { isin nonTradeSecurityTransactionType quantity amount }
      ... on BrokerEltifTransactionSummary { isin securityTransactionType eltifQuantity amount side } } } } } }"""
Q_OVERNIGHT_DISCOVER = """query DiscoverOvernightAccounts($accountId: ID!) {
  account(id: $accountId) { savingsAccounts { __typename id state personalizations { name } } } }"""
Q_OVERNIGHT_SUMMARY = """query OvernightSummary($accountId: ID!, $savingsAccountId: ID!) {
  account(id: $accountId) { savingsAccount(id: $savingsAccountId) { id
    ... on OvernightSavingsAccount { totalAmount nextPayoutDate { epochSecond }
      interests { currentAccruedAmount depositInterestRate estimatedNextPayoutAmount } } } } }"""
Q_OVERNIGHT_TX = """query OvernightTransactions($accountId: ID!, $savingsAccountId: ID!, $input: SavingsAccountCashTransactionInput!) {
  account(id: $accountId) { savingsAccount(id: $savingsAccountId) { id moreTransactions(input: $input) {
    cursor total transactions { id currency type status isCancellation lastEventDateTime description
      cashTransactionType amount custodian relatedIsin } } } } }"""

report: dict = {"user_agent": USER_AGENT, "steps": []}


def note(step: str, **data) -> None:
    print(f"[{step}] {json.dumps(data, default=str)[:400]}")
    report["steps"].append({"step": step, "at": time.time(), **data})


def write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))
    path.chmod(0o600)


def claims(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


def claim(c: dict, name: str):
    return c.get(f"https://de.scalable.capital/{name}") or c.get(name)


async def post(http: aiohttp.ClientSession, key: DpopKey, url: str, *, form=None, body=None, token=None):
    """One DPoP request with the CLI's single nonce retry."""
    nonce = None
    for attempt in range(2):
        headers = {"DPoP": key.proof("POST", url, nonce=nonce, access_token=token), "User-Agent": USER_AGENT}
        if token:
            headers["Authorization"] = f"DPoP {token}"
        async with http.post(url, data=form, json=body, headers=headers) as r:
            text = await r.text()
            if r.status >= 400 and attempt == 0 and r.headers.get("DPoP-Nonce") and "nonce" in text:
                nonce = r.headers["DPoP-Nonce"]
                note("dpop-nonce-challenge", url=url)
                continue
            return r.status, dict(r.headers), text
    raise AssertionError("unreachable")


async def gql(http, key, token, op, query, variables):
    status, headers, text = await post(http, key, GRAPHQL, body={"operationName": op, "query": query, "variables": variables}, token=token)
    data = json.loads(text) if text.startswith("{") else {"raw": text}
    write(OUT / f"gql-{op}.json", {"status": status, "variables": variables, "response": data})
    note(f"gql:{op}", status=status, errors=(data.get("errors") if isinstance(data, dict) else None))
    return data.get("data") if isinstance(data, dict) else None


async def token_request(http, key, form):
    status, headers, text = await post(http, key, f"{ISSUER}/oauth/token", form={**form, "client_id": CLIENT_ID})
    return status, json.loads(text) if text.startswith("{") else {"raw": text}


def remember(session: dict, tok: dict) -> None:
    c = claims(tok["access_token"])
    session.update(
        access_token=tok["access_token"],
        refresh_token=tok.get("refresh_token") or session.get("refresh_token"),
        expires_at=time.time() + tok.get("expires_in", c["exp"] - time.time()),
        person_id=claim(c, "person_id"),
        session_id=claim(c, "session_id"),
    )
    note(
        "token",
        expires_in=tok.get("expires_in"),
        access_ttl=c.get("exp", 0) - c.get("iat", 0),
        got_refresh_token=bool(tok.get("refresh_token")),
        claim_names=sorted(c.keys()),
        cnf_jkt_matches=(c.get("cnf", {}).get("jkt") == session["dpop_jkt"]),
    )
    write(SESSION, session)


async def login(http) -> dict:
    key = DpopKey.generate()
    session = {"dpop_pem": key.to_pem(), "dpop_jkt": key.thumbprint, "logged_in_at": time.time()}
    status, _, text = await post(
        http, key, f"{ISSUER}/oauth/device/code",
        form={"client_id": CLIENT_ID, "audience": AUDIENCE, "scope": SCOPE},
    )
    note("device-code", status=status)
    if status >= 400:
        note("device-code-failed", body=text[:1000])
        raise SystemExit("device code request refused, see report.json")
    device = json.loads(text)
    print("\n  Open:", device.get("verification_uri_complete") or device["verification_uri"])
    print("  Code:", device["user_code"], "\n")
    interval = device.get("interval", 5)
    deadline = time.time() + device["expires_in"]
    while time.time() < deadline:
        await asyncio.sleep(interval)
        status, tok = await token_request(http, key, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": device["device_code"],
        })
        if status < 400:
            remember(session, tok)
            break
        if tok.get("error") == "authorization_pending":
            continue
        if tok.get("error") == "slow_down":
            interval += 2
            continue
        note("device-poll-failed", status=status, body=tok)
        raise SystemExit("device login failed, see report.json")
    else:
        raise SystemExit("device code expired")

    uid = session["person_id"]
    state = await gql(http, key, session["access_token"], "Is2faOnLoginEnabled", Q_2FA_STATE, {"input": {"userId": uid}})
    state = (state or {}).get("is2faOnLoginEnabled") or {}
    if state.get("enabled") and not state.get("hasApprovedSession"):
        started = await gql(http, key, session["access_token"], "Start2faOnLogin", M_2FA_START,
                            {"input": {"userId": uid, "deviceName": "CLI", "deviceType": "CLI"}})
        mfa = started["start2faOnLogin"]["mfaSessionId"]
        print("  Approve the login on your trusted device...")
        for _ in range(60):
            res = await gql(http, key, session["access_token"], "Validate2faOnLogin", M_2FA_VALIDATE,
                            {"input": {"userId": uid, "mfaSessionId": mfa}})
            st = res["validate2faOnLogin"]["status"]
            if st == "SUCCESS":
                break
            if st != "PENDING":
                raise SystemExit(f"2FA failed: {st}")
            await asyncio.sleep(2)
    return session


async def refresh(http, session) -> dict:
    key = DpopKey.from_pem(session["dpop_pem"])
    form = {"grant_type": "refresh_token", "refresh_token": session["refresh_token"]}
    if session.get("session_id"):
        form["session_id"] = session["session_id"]
    status, tok = await token_request(http, key, form)
    note("refresh", status=status, age_hours=round((time.time() - session["logged_in_at"]) / 3600, 2),
         error=tok.get("error") if status >= 400 else None,
         rotated=bool(tok.get("refresh_token")) and tok.get("refresh_token") != session["refresh_token"])
    if status >= 400:
        raise SystemExit("refresh failed, see report.json")
    remember(session, tok)
    return session


async def queries(http, session) -> None:
    key = DpopKey.from_pem(session["dpop_pem"])
    tok, uid = session["access_token"], session["person_id"]
    ids = await gql(http, key, tok, "ResolveBrokerIds", Q_RESOLVE, {"id": uid})
    portfolios = [p["id"] for p in ((ids or {}).get("account") or {}).get("brokerPortfolios") or []]
    note("portfolios", count=len(portfolios))
    for pid in portfolios[:1]:
        base = {"accountId": uid, "portfolioId": pid}
        await gql(http, key, tok, "BrokerOverview", Q_OVERVIEW, {**base, "includeYearToDate": True})
        await gql(http, key, tok, "BrokerHoldings", Q_HOLDINGS, {**base, "includeYearToDate": False, "quoteSource": None})
        await gql(http, key, tok, "BrokerLimits", Q_LIMITS, base)
        await gql(http, key, tok, "BrokerTransactions", Q_TRANSACTIONS,
                  {**base, "input": {"pageSize": 50, "cursor": None, "includeReinvestmentSubtypes": True}})
    disc = await gql(http, key, tok, "DiscoverOvernightAccounts", Q_OVERNIGHT_DISCOVER, {"accountId": uid})
    for acc in ((disc or {}).get("account") or {}).get("savingsAccounts") or []:
        if acc.get("__typename") == "OvernightSavingsAccount":
            sid = {"accountId": uid, "savingsAccountId": acc["id"]}
            await gql(http, key, tok, "OvernightSummary", Q_OVERNIGHT_SUMMARY, sid)
            await gql(http, key, tok, "OvernightTransactions", Q_OVERNIGHT_TX,
                      {**sid, "input": {"pageSize": 50, "cursor": None}})
            break


async def main(mode: str) -> None:
    OUT.mkdir(mode=0o700, exist_ok=True)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        try:
            if mode == "login":
                session = await login(http)
                await queries(http, session)
                session = await refresh(http, session)
                await queries(http, session)
            else:
                session = json.loads(SESSION.read_text())
                session = await refresh(http, session)
                await queries(http, session)
        finally:
            prev = json.loads(REPORT.read_text()) if REPORT.exists() else {"runs": []}
            prev["runs"].append(report)
            write(REPORT, prev)
    print("\nDone. Report:", REPORT)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "login"))

"""A fake of the public market-data APIs bank prices securities with: OpenFIGI, Yahoo, Twelve Data.

* OpenFIGI  ``POST /v3/mapping``                 ISIN → listings (``ticker``, ``exchCode``, ``name``)
* Yahoo     ``GET  /v8/finance/chart/{symbol}``  daily closes (``period1``/``period2`` or ``range``) + ``meta``
* Twelve Data ``GET /time_series``, ``GET /quote`` — refuses without ``apikey`` (as the real one)

Seeded per test through ``/_admin``:

* ``POST /_admin/listings``  ``{isin: [{"ticker", "exchCode", "name"}]}``
* ``POST /_admin/series``    ``{"YAHOO:VWCE.DE" | "TWELVEDATA:VWCE@XETR": {"currency", "points": [[day, close]]}}``
* ``POST /_admin/reset``, ``GET /_admin/log``
"""

import datetime

from aiohttp import web

STATE: dict = {"listings": {}, "series": {}, "log": []}
API_KEY = "test-twelvedata-key"


def _log(request: web.Request, **extra) -> None:
    STATE["log"].append({"path": request.path, "query": dict(request.query), **extra})


async def figi(request: web.Request) -> web.Response:
    jobs = await request.json()
    _log(request, jobs=jobs)
    return web.json_response([{"data": STATE["listings"][job["idValue"]]} if job["idValue"] in STATE["listings"] else {"warning": "No identifier found."} for job in jobs])


def _window(points: list, start: datetime.date | None, end: datetime.date | None) -> list:
    return [(d, c) for d, c in points if (start is None or datetime.date.fromisoformat(d) >= start) and (end is None or datetime.date.fromisoformat(d) <= end)]


async def yahoo_chart(request: web.Request) -> web.Response:
    symbol = request.match_info["symbol"]
    _log(request, symbol=symbol)
    series = STATE["series"].get(f"YAHOO:{symbol}")
    if series is None:
        return web.json_response({"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"}}}, status=404)
    q = request.query
    start = datetime.datetime.fromtimestamp(int(q["period1"]), datetime.timezone.utc).date() if "period1" in q else None
    end = datetime.datetime.fromtimestamp(int(q["period2"]), datetime.timezone.utc).date() - datetime.timedelta(days=1) if "period2" in q else None
    points = _window(series["points"], start, end) if start else series["points"][-1:]
    stamps = [int(datetime.datetime.combine(datetime.date.fromisoformat(d), datetime.time(7), tzinfo=datetime.timezone.utc).timestamp()) for d, _ in points]
    last = series["points"][-1]
    meta = {"currency": series["currency"], "symbol": symbol, "longName": series.get("name"), "regularMarketPrice": last[1], "regularMarketTime": stamps[-1] if stamps else None}
    return web.json_response({"chart": {"result": [{"meta": meta, "timestamp": stamps, "indicators": {"quote": [{"close": [c for _, c in points]}]}}], "error": None}})


def _td(request: web.Request):  # noqa: ANN202
    q = request.query
    _log(request)
    if q.get("apikey") != API_KEY:
        return None, web.json_response({"code": 401, "message": "**apikey** parameter is incorrect or not specified.", "status": "error"})
    series = STATE["series"].get(f"TWELVEDATA:{q['symbol']}@{q.get('mic_code', '')}")
    if series is None:
        return None, web.json_response({"code": 404, "message": "**symbol** not found", "status": "error"})
    return series, None


async def td_series(request: web.Request) -> web.Response:
    series, error = _td(request)
    if error:
        return error
    q = request.query
    points = _window(series["points"], datetime.date.fromisoformat(q["start_date"]), datetime.date.fromisoformat(q["end_date"]))
    return web.json_response({"meta": {"symbol": q["symbol"], "currency": series["currency"], "mic_code": q.get("mic_code")}, "values": [{"datetime": d, "close": str(c)} for d, c in points], "status": "ok"})


async def td_quote(request: web.Request) -> web.Response:
    series, error = _td(request)
    if error:
        return error
    day, close = series["points"][-1]
    return web.json_response({"symbol": request.query["symbol"], "name": series.get("name"), "currency": series["currency"], "close": str(close), "timestamp": int(datetime.datetime.fromisoformat(day).replace(tzinfo=datetime.timezone.utc).timestamp())})


async def admin_listings(request: web.Request) -> web.Response:
    STATE["listings"].update(await request.json())
    return web.json_response({"ok": True})


async def admin_series(request: web.Request) -> web.Response:
    STATE["series"].update(await request.json())
    return web.json_response({"ok": True})


async def admin_reset(request: web.Request) -> web.Response:
    STATE["listings"].clear()
    STATE["series"].clear()
    return web.json_response({"ok": True})


async def admin_log(request: web.Request) -> web.Response:
    return web.json_response({"log": STATE["log"]})


def build() -> web.Application:
    app = web.Application()
    app.add_routes(
        [
            web.post("/v3/mapping", figi),
            web.get("/v8/finance/chart/{symbol}", yahoo_chart),
            web.get("/time_series", td_series),
            web.get("/quote", td_quote),
            web.post("/_admin/listings", admin_listings),
            web.post("/_admin/series", admin_series),
            web.post("/_admin/reset", admin_reset),
            web.get("/_admin/log", admin_log),
            web.get("/_admin/health", lambda request: web.json_response({"ok": True})),
        ]
    )
    return app


if __name__ == "__main__":
    web.run_app(build(), host="0.0.0.0", port=8000, print=None)

"""A fake Nominatim (OpenStreetMap geocoder) for the bank service's test suite.

Answers ``/search`` (free text ``q`` or structured street/postalcode/city) and ``/reverse`` in
``format=jsonv2`` with ``addressdetails``, from places the tests register. Like the real server's
usage policy it refuses requests without an identifying User-Agent.

* ``POST /_admin/places``  add places: ``[{"name", "lat", "lon", "road", "house_number", "postcode", "city", "state", "country_code", "osm_id"}]``
* ``POST /_admin/reset``   forget all places
* ``GET  /_admin/log``     every request (path, params, user agent)
"""

import math

from aiohttp import web

STATE: dict = {"places": [], "log": []}


def _hit(place: dict) -> dict:
    return {
        "osm_type": "node",
        "osm_id": place.get("osm_id", 1),
        "lat": str(place["lat"]),
        "lon": str(place["lon"]),
        "display_name": ", ".join(str(place[k]) for k in ("name", "road", "city") if place.get(k)),
        "address": {k: place[k] for k in ("road", "house_number", "postcode", "city", "state", "country_code") if place.get(k)},
    }


def _ua_ok(request: web.Request) -> bool:
    agent = request.headers.get("User-Agent", "")
    STATE["log"].append({"path": request.path, "params": dict(request.query), "user_agent": agent})
    return bool(agent) and not agent.startswith("Python/")


async def search(request: web.Request) -> web.Response:
    if not _ua_ok(request):
        return web.json_response({"error": "set a User-Agent"}, status=403)
    q = request.query
    words = (q.get("q") or " ".join(q.get(k, "") for k in ("street", "postalcode", "city"))).lower().replace(",", " ").split()
    hits = []
    for place in STATE["places"]:
        haystack = " ".join(str(place.get(k, "")) for k in ("name", "road", "house_number", "postcode", "city")).lower()
        if words and all(word in haystack for word in words):
            hits.append(_hit(place))
    return web.json_response(hits[: int(q.get("limit", 10))])


async def reverse(request: web.Request) -> web.Response:
    if not _ua_ok(request):
        return web.json_response({"error": "set a User-Agent"}, status=403)
    lat, lon = float(request.query["lat"]), float(request.query["lon"])
    if not STATE["places"]:
        return web.json_response({"error": "Unable to geocode"})
    nearest = min(STATE["places"], key=lambda p: math.hypot(p["lat"] - lat, p["lon"] - lon))
    return web.json_response(_hit(nearest))


async def admin_places(request: web.Request) -> web.Response:
    STATE["places"].extend(await request.json())
    return web.json_response({"ok": True})


async def admin_reset(request: web.Request) -> web.Response:
    STATE["places"].clear()
    return web.json_response({"ok": True})


async def admin_log(request: web.Request) -> web.Response:
    return web.json_response({"log": STATE["log"]})


def build() -> web.Application:
    app = web.Application()
    app.add_routes(
        [
            web.get("/search", search),
            web.get("/reverse", reverse),
            web.post("/_admin/places", admin_places),
            web.post("/_admin/reset", admin_reset),
            web.get("/_admin/log", admin_log),
            web.get("/_admin/health", lambda request: web.json_response({"ok": True})),
        ]
    )
    return app


if __name__ == "__main__":
    web.run_app(build(), host="0.0.0.0", port=8000, print=None)

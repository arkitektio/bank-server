"""Address ↔ coordinates through a Nominatim (OpenStreetMap) server — only when a request asks.

Nothing geocodes on its own: a client calls ``geocodeSearch`` (to fill a picker) or
``geocodeMerchantLocation`` (to fill one location), and the lookup happens inside that request.
Nominatim's usage policy wants an identifying User-Agent and at most one request a second;
one lookup per user action stays well within it.
"""

from dataclasses import dataclass
from decimal import Decimal

import aiohttp
from django.conf import settings


class GeocodingError(Exception):
    """The geocoder could not be reached or refused."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class GeocodingDisabled(Exception):
    """This deployment has geocoding switched off."""

    def __init__(self) -> None:
        super().__init__("Geocoding is disabled on this server (`geocoding.enabled`).")


@dataclass
class Place:
    """One geocoder hit."""

    label: str
    latitude: Decimal
    longitude: Decimal
    street: str | None
    postal_code: str | None
    city: str | None
    region: str | None
    country: str | None
    osm_id: str | None


def _conf() -> dict:
    conf = getattr(settings, "GEOCODING", None) or {}
    if not conf.get("enabled", False):
        raise GeocodingDisabled()
    return conf


def _place(hit: dict) -> Place:
    address = hit.get("address") or {}
    street = " ".join(part for part in (address.get("road") or address.get("pedestrian"), address.get("house_number")) if part) or None
    osm = f"{hit.get('osm_type', '')[:1].upper()}{hit['osm_id']}" if hit.get("osm_id") else None
    return Place(
        label=hit.get("display_name") or "",
        latitude=Decimal(str(hit["lat"])).quantize(Decimal("0.000001")),
        longitude=Decimal(str(hit["lon"])).quantize(Decimal("0.000001")),
        street=street,
        postal_code=address.get("postcode"),
        city=address.get("city") or address.get("town") or address.get("village") or address.get("municipality"),
        region=address.get("state"),
        country=(address.get("country_code") or "").upper() or None,
        osm_id=osm,
    )


async def _get(path: str, params: dict) -> list[dict] | dict:
    conf = _conf()
    url = conf["url"].rstrip("/") + path
    headers = {"User-Agent": conf["user_agent"], "Accept-Language": conf.get("language", "de,en")}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=conf.get("timeout_seconds", 10))) as http:
            async with http.get(url, params={**params, "format": "jsonv2", "addressdetails": "1"}, headers=headers) as response:
                if response.status >= 400:
                    raise GeocodingError(f"Geocoder answered HTTP {response.status}.", response.status)
                return await response.json(content_type=None)
    except aiohttp.ClientError as error:
        raise GeocodingError(f"Geocoder unreachable: {error}") from error


async def search(query: str | None = None, *, street: str | None = None, postal_code: str | None = None, city: str | None = None, country: str | None = None, limit: int = 5) -> list[Place]:
    """Places matching a free-text query or a structured address, best first."""
    conf = _conf()
    params: dict = {"limit": str(max(1, min(limit, 20)))}
    if conf.get("country_codes"):
        params["countrycodes"] = conf["country_codes"]
    if query:
        params["q"] = query
    else:
        params.update({k: v for k, v in (("street", street), ("postalcode", postal_code), ("city", city), ("country", country)) if v})
        if len(params) <= 2:
            return []
    hits = await _get("/search", params)
    return [_place(hit) for hit in hits if isinstance(hit, dict) and "lat" in hit]


async def reverse(latitude: float, longitude: float) -> Place | None:
    """The address at a coordinate, if the geocoder knows one."""
    hit = await _get("/reverse", {"lat": str(latitude), "lon": str(longitude)})
    return _place(hit) if isinstance(hit, dict) and "lat" in hit else None

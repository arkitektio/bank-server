"""PostGIS without GeoDjango: one geography column type and the few functions bank needs.

GeoDjango needs GDAL and GEOS in every process that imports it — the image and every host
that runs the suite. Bank only stores points and asks "within r meters of here" and "how far",
which PostGIS answers on its own. So the point is a *generated* ``geography(Point,4326)``
column computed by Postgres from plain ``latitude``/``longitude`` columns (which is what the API
reads and writes), GiST-indexed, and queried through thin ``Func`` wrappers. Python never
parses geometry.
"""

from typing import Any

from django.db import models
from django.db.models import BooleanField, F, FloatField, Func, Value


class GeographyPointField(models.Field):
    """A ``geography(Point,4326)`` column. Values stay opaque (hex WKB); read latitude/longitude instead."""

    description = "A WGS84 point (PostGIS geography)"

    def db_type(self, connection: Any) -> str:
        return "geography(Point,4326)"

    def rel_db_type(self, connection: Any) -> str:
        return self.db_type(connection)


class MakePoint(Func):
    """``ST_MakePoint(lon, lat)`` as a WGS84 geography — NULL if either is NULL. Immutable, so usable in a generated column."""

    template = "ST_SetSRID(ST_MakePoint(%(expressions)s), 4326)::geography"
    arity = 2
    output_field = GeographyPointField()


def point(longitude: float, latitude: float) -> MakePoint:
    """A literal point, for comparing a column against."""
    return MakePoint(Value(float(longitude), output_field=FloatField()), Value(float(latitude), output_field=FloatField()))


class DistanceMeters(Func):
    """Geodesic distance in meters between two geographies."""

    function = "ST_Distance"
    arity = 2
    output_field = FloatField()


class DWithin(Func):
    """Whether two geographies are within ``meters`` of each other (uses the GiST index)."""

    function = "ST_DWithin"
    arity = 3
    output_field = BooleanField()


def near(prefix: str, field: str, latitude: float, longitude: float, radius_meters: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """The annotations and the filter kwargs for "``prefix+field`` within ``radius_meters`` of (lat, lon)".

    Returns ``(annotations, filter)`` so a strawberry filter can annotate the distance (clients
    read it as ``distanceMeters``) and filter on the index-backed ``ST_DWithin``.
    """
    column = F(f"{prefix}{field}")
    here = point(longitude, latitude)
    return (
        {"_distance_meters": DistanceMeters(column, here), "_within": DWithin(column, here, Value(float(radius_meters), output_field=FloatField()))},
        {"_within": True},
    )


def nearest_location_meters(latitude: float, longitude: float, outer: str = "pk") -> Any:
    """A subquery: the distance from (lat, lon) to the merchant's (``OuterRef(outer)``) closest located store."""
    from django.db.models import OuterRef, Subquery

    from finance import models

    distances = (
        models.MerchantLocation.objects.filter(merchant=OuterRef(outer), point__isnull=False)
        .annotate(_d=DistanceMeters(F("point"), point(longitude, latitude)))
        .order_by("_d")
        .values("_d")[:1]
    )
    return Subquery(distances, output_field=FloatField())


class Envelope(Func):
    """A lon/lat rectangle as a geography: ``ST_MakeEnvelope(west, south, east, north, 4326)``."""

    template = "ST_MakeEnvelope(%(expressions)s, 4326)::geography"
    arity = 4
    output_field = GeographyPointField()


class Intersects(Func):
    """Whether two geographies intersect (uses the GiST index)."""

    function = "ST_Intersects"
    arity = 2
    output_field = BooleanField()


class AsGeoJSON(Func):
    """A geography as a GeoJSON geometry (text), coordinates rounded to 6 places (~0.1 m)."""

    template = "ST_AsGeoJSON(%(expressions)s, 6)"
    arity = 1
    output_field = models.TextField()


def within(prefix: str, field: str, south: float, west: float, north: float, east: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Annotations and filter kwargs for "``prefix+field`` inside the lat/lon box" — a map's viewport."""
    box = Envelope(*(Value(float(v), output_field=FloatField()) for v in (west, south, east, north)))
    return {"_in_bounds": Intersects(F(f"{prefix}{field}"), box)}, {"_in_bounds": True}


class AsGeometry(Func):
    """A geography as a (lon/lat) geometry — what grid snapping works on."""

    template = "(%(expressions)s)::geometry"
    arity = 1
    output_field = GeographyPointField()


class SnapToGrid(Func):
    """``ST_SnapToGrid(geometry, dx, dy)``: the point moved to the nearest grid node (degrees)."""

    function = "ST_SnapToGrid"
    arity = 3
    output_field = GeographyPointField()

    def __init__(self, geometry: Any, dx: float, dy: float, **extra: Any) -> None:
        super().__init__(geometry, Value(float(dx), output_field=FloatField()), Value(float(dy), output_field=FloatField()), **extra)


class PointX(Func):
    """The longitude of a point geometry."""

    function = "ST_X"
    arity = 1
    output_field = FloatField()


class PointY(Func):
    """The latitude of a point geometry."""

    function = "ST_Y"
    arity = 1
    output_field = FloatField()

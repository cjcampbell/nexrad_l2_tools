"""anchors.py

The instant a download window is measured from.

A radar archive serves many questions, and each has its own natural reference. Emergence
work measures from sunset; a dawn study from sunrise; a convection study from local
afternoon; a whole-day pull from UTC midnight. Hard-coding any one of them makes the
archive's tooling an instrument of one project, so the anchor is chosen per run and
recorded per scan.

Anchors
-------
utc_midnight     00:00 UTC on the given date. The default, and the only one that needs
                 neither a timezone nor an ephemeris: offsets 0 to 1440 are the UTC day
                 exactly as the archive files it.
local_midnight   00:00 civil time at the station, converted to UTC. DST-aware through
                 `zoneinfo`, so it is the clock on the wall, not a fixed offset.
sunrise, sunset  Refraction-corrected, upper limb touching the horizon (-0.833 deg).
solar_noon       Solar transit: the sun's highest point, which is not 12:00 local.
solar_midnight   Antitransit, defined here as solar noon on the given date plus 12 hours.

Standard library only. The solar terms are the NOAA algorithm carried by `solar.py`,
generalised from sunset to transit and both horizon crossings; `check_against_solar_py()`
asserts this file reproduces that module's sunset exactly, so the two cannot drift.
"""

import datetime as dt
import math
from zoneinfo import ZoneInfo

# Centre of the disc sits 0.833 deg below the horizon when the upper limb appears to
# touch it: the standard refraction correction, and the value solar.py uses.
_HORIZON_ELEVATION_DEG = -0.833

ANCHORS = ("utc_midnight", "local_midnight", "sunrise", "sunset",
           "solar_noon", "solar_midnight")

SOLAR_ANCHORS = ("sunrise", "sunset", "solar_noon", "solar_midnight")


def _solar_terms(date: dt.date, lon: float) -> tuple[float, float, float]:
    """(transit in Julian days, declination in radians, mean anomaly in radians)."""
    n = date.toordinal() - dt.date(2000, 1, 1).toordinal() - lon / 360.0
    j = 2451545.0 + n

    mean_anomaly = math.radians((357.5291 + 0.98560028 * (j - 2451545.0)) % 360)
    center = (1.9148 * math.sin(mean_anomaly)
              + 0.0200 * math.sin(2 * mean_anomaly)
              + 0.0003 * math.sin(3 * mean_anomaly))
    ecliptic_lon = math.radians(
        (math.degrees(mean_anomaly) + center + 180 + 102.9372) % 360
    )

    transit = j + 0.0053 * math.sin(mean_anomaly) - 0.0069 * math.sin(2 * ecliptic_lon)
    declination = math.asin(math.sin(ecliptic_lon) * math.sin(math.radians(23.44)))
    return transit, declination, mean_anomaly


def _from_julian(j: float) -> dt.datetime:
    return (dt.datetime(2000, 1, 1, 12, tzinfo=dt.timezone.utc)
            + dt.timedelta(days=j - 2451545.0))


def _hour_angle(lat: float, declination: float) -> float | None:
    """Degrees of rotation from transit to the horizon crossing, or None in polar day."""
    lat_rad = math.radians(lat)
    cos_hour_angle = (
        (math.sin(math.radians(_HORIZON_ELEVATION_DEG))
         - math.sin(lat_rad) * math.sin(declination))
        / (math.cos(lat_rad) * math.cos(declination))
    )
    if abs(cos_hour_angle) > 1:
        return None
    return math.degrees(math.acos(cos_hour_angle))


def anchor_utc(kind: str, date: dt.date, lat: float, lon: float,
               tz: str | None = None) -> dt.datetime | None:
    """The anchor instant, in UTC. None where the sun does not rise or set that day."""
    if kind == "utc_midnight":
        return dt.datetime(date.year, date.month, date.day, tzinfo=dt.timezone.utc)

    if kind == "local_midnight":
        if not tz:
            raise ValueError("local_midnight needs a station timezone")
        local = dt.datetime(date.year, date.month, date.day, tzinfo=ZoneInfo(tz))
        return local.astimezone(dt.timezone.utc)

    if kind not in SOLAR_ANCHORS:
        raise ValueError(f"unknown anchor {kind!r}; expected one of {ANCHORS}")

    transit, declination, _ = _solar_terms(date, lon)
    if kind == "solar_noon":
        return _from_julian(transit)
    if kind == "solar_midnight":
        return _from_julian(transit + 0.5)

    hour_angle = _hour_angle(lat, declination)
    if hour_angle is None:
        return None
    offset = hour_angle / 360.0
    return _from_julian(transit + offset if kind == "sunset" else transit - offset)


def check_against_solar_py() -> int:
    """Assert this module's sunset equals solar.py's, which is the verified one.

    Two implementations of the same algorithm is how a subtle divergence gets into an
    archive that many projects read. Called by the tool's self-test.
    """
    from solar import sunset_utc

    cases = [
        (dt.date(2020, 8, 25), 29.7039, -98.0289),   # KEWX
        (dt.date(2006, 1, 1), 29.2730, -100.2800),   # KDFX, midwinter
        (dt.date(2017, 6, 19), 30.7219, -98.0272),   # KGRK, solstice
        (dt.date(2011, 3, 29), 31.3713, -100.4925),  # KSJT, equinox
        (dt.date(2025, 12, 21), 45.4558, -98.4132),  # KABR, high latitude
    ]
    for date, lat, lon in cases:
        mine = anchor_utc("sunset", date, lat, lon)
        theirs = sunset_utc(date, lat, lon)
        if mine != theirs:
            raise AssertionError(
                f"sunset disagrees at {date} ({lat}, {lon}): {mine} vs {theirs}")
    return len(cases)


if __name__ == "__main__":
    n = check_against_solar_py()
    print(f"anchors.sunset matches solar.sunset_utc exactly on {n} cases")
    for kind in ANCHORS:
        tz = "America/Chicago" if kind == "local_midnight" else None
        when = anchor_utc(kind, dt.date(2020, 8, 25), 29.7039, -98.0289, tz)
        print(f"  {kind:16s} {when:%Y-%m-%d %H:%M:%SZ}")

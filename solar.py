"""Sunset times for the emergence window.

The screened labels carry a `from_sunset` column, but it exists only on rows the
detector produced. A night with no bats has almost no rows, so sunset cannot be
recovered from the labels on exactly the nights the zero counts come from. This
module computes it from date and position instead, which works for every night
whether or not anything was detected.

NOAA solar position algorithm, no ephemeris files and no network. Vendored from
`../TABR radar data/analyses/05_nightly_flux/sunset.py` and re-verified here against
the `from_sunset` column of the screened labels; see docs/DECISIONS.md.
"""

import datetime as dt
import math

# Standard refraction-corrected solar elevation for sunset: the center of the disc
# sits 0.833 degrees below the horizon when the upper limb appears to touch it.
_SUNSET_ELEVATION_DEG = -0.833


def sunset_utc(date: dt.date, lat: float, lon: float) -> dt.datetime | None:
    """UTC time of sunset on a local calendar date at (lat, lon).

    Returns None inside a polar day or night, where the sun does not set. That
    cannot arise in Texas, but the caller should not have to assume it.
    """
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

    lat_rad = math.radians(lat)
    cos_hour_angle = (
        (math.sin(math.radians(_SUNSET_ELEVATION_DEG))
         - math.sin(lat_rad) * math.sin(declination))
        / (math.cos(lat_rad) * math.cos(declination))
    )
    if abs(cos_hour_angle) > 1:
        return None

    hour_angle = math.degrees(math.acos(cos_hour_angle))
    j_set = transit + hour_angle / 360.0
    return (dt.datetime(2000, 1, 1, 12, tzinfo=dt.timezone.utc)
            + dt.timedelta(days=j_set - 2451545.0))

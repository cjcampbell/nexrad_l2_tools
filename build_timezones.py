"""build_timezones.py

Regenerate `station_timezones.csv` from `nexrad_stations.csv`.

A BUILD-TIME script, not part of the tool. `fetch_scans.py` needs a station's IANA zone
only for `--anchor local_midnight`, and resolving a coordinate to a zone needs
`timezonefinder` - a heavy dependency with its own data files. Since NEXRAD stations do
not move, the lookup is done once here and the answer committed as a small CSV, leaving
the tool itself on the standard library, where `zoneinfo` handles the rest.

Run it when `nexrad_stations.csv` gains a station, or when you want to refresh the zones
against a newer `timezonefinder` (boundaries do occasionally change - a country redraws
a zone, and an IANA release follows).

    pip install timezonefinder
    python3 build_timezones.py

It prints a summary and rewrites `station_timezones.csv` in place. Review the diff before
committing: a zone that changes without a station moving is worth understanding, not
waving through.
"""

import csv
from pathlib import Path

from timezonefinder import TimezoneFinder

HERE = Path(__file__).resolve().parent
STATIONS = HERE / "nexrad_stations.csv"
TIMEZONES = HERE / "station_timezones.csv"


def main() -> None:
    finder = TimezoneFinder()
    with open(STATIONS, newline="") as handle:
        stations = list(csv.DictReader(handle))

    rows, unresolved = [], []
    for station in stations:
        zone = finder.timezone_at(lat=float(station["lat"]), lng=float(station["lon"]))
        if not zone:
            unresolved.append(station["station_id"])
        rows.append({"station_id": station["station_id"], "tz": zone or ""})

    with open(TIMEZONES, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["station_id", "tz"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"{len(rows)} stations written to {TIMEZONES.name}")
    if unresolved:
        # A blank zone is not fatal - it only disables local_midnight for that station -
        # but it is always worth knowing about.
        print(f"WARNING: no timezone resolved for {len(unresolved)}: {unresolved}")


if __name__ == "__main__":
    main()

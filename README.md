# nexrad_l2_tools

Tools for building and sharing an archive of NEXRAD Level II radar volumes.

One archive, several projects. The tools here fetch volumes into a shared tree and
record who fetched what, when, and for which project — so a scan downloaded for one
study can be reused by another without anyone having to reconstruct where it came from.

**Standard library only, Python 3.10+.** No virtualenv, no conda, no `pip install`. That
is deliberate: a shared tool that needs an environment is a tool most people will not
use.

## Quick start

```sh
# what would be fetched? (always start here)
python3 fetch_scans.py --dates days.csv --user YOUR_NETID --project my_project \
    --archive /path/to/nexrad_l2_data --dry-run

# a whole UTC day per row — the default
python3 fetch_scans.py --dates days.csv --user YOUR_NETID --project my_project \
    --archive /path/to/nexrad_l2_data
```

where `days.csv` is:

```csv
station,date
KEWX,20200825
KDFX,2020-08-26
```

`--user` and `--project` are required. Nothing is fetched anonymously into a shared
archive.

`--archive` is required too, and has no default. Set it once in your shell if you tire of
typing it:

```sh
export NEXRAD_L2_ARCHIVE=/path/to/nexrad_l2_data
```

An earlier draft derived a default from the tool's own location, which pointed a fresh
clone at a directory nobody meant to write to. Guessing where several TB of radar data
should land is not a favour.

## Choosing when

The archive has no house opinion about when a day starts. A period is an **anchor** plus
offsets in minutes:

```sh
--anchor utc_midnight   --from-min    0 --to-min 1440   # the UTC day (default)
--anchor sunset         --from-min -180 --to-min   60   # bat emergence
--anchor sunrise        --from-min  -60 --to-min   60   # dawn flight
--anchor local_midnight --from-min -360 --to-min  360   # 18:00–06:00 local
--anchor solar_noon     --from-min -120 --to-min  120   # afternoon convection
--anchor solar_midnight --from-min  -30 --to-min   30
```

`utc_midnight` is the default because it needs neither a timezone nor an ephemeris: it
is the day exactly as the archive files it.

Per-row overrides live in the CSV (`anchor`, `from_min`, `to_min`), and `start_utc` /
`end_utc` columns state an absolute interval and bypass anchoring entirely. That is how
a project keeps its own rule — a seasonal window, say — without pushing it into shared
infrastructure. Full column list in [SCHEMAS.md](SCHEMAS.md).

## What it writes

```
<archive>/
  scans/YYYY/MM/DD/STATION/<scan>   mirrors the upstream S3 key structure exactly
  index/<run_id>.csv                one row per volume a run touched
  selections/<hash>.csv             the request itself, addressed by its contents
  acquisitions.csv                  one row per run: who, when, which project, which code
  logs/<run_id>.log                 free text, for debugging
```

Three rules, because the archive is shared:

1. **Nothing is deleted or overwritten.** A volume already on disk is left exactly as it
   is, whoever put it there.
2. **Downloads are atomic.** Bytes land in a `.part` file and are renamed into place only
   once the payload matches `Content-Length`. An interrupted run leaves no short file for
   the next project to mistake for a whole volume.
3. **Every run identifies itself**, and the ledger records the checksum of the code that
   ran, so a scan can be traced to the person, project and version that asked for it.
   The selection file is kept too, so the request survives even when the file that
   expressed it is later edited.

A single row may not ask for more than 31 days of volumes. That is nearly always a
mistyped offset, and the cost of one falls on the upstream bucket; express a genuinely
long period as several rows.

## Files

| file | what it is |
|---|---|
| `fetch_scans.py` | the tool |
| `anchors.py` | the six anchors; self-verifies against `solar.py` when run directly |
| `nexrad_io.py` | upstream listing, key resolution and download primitives |
| `solar.py` | NOAA sunset, the reference implementation `anchors.py` checks itself against |
| `nexrad_stations.csv` | 155 NEXRAD stations: id, name, state, lat, lon, elevation |
| `station_timezones.csv` | station → IANA zone; see below |
| `build_timezones.py` | regenerates the above; build-time only |
| `SCHEMAS.md` | the record-keeping formats, in detail |

`nexrad_io.py` and `solar.py` are vendored from the `TABRadar_drivers` project and are
byte-identical to their originals there. Keeping them identical is what makes `diff` a
useful drift check, so prefer adding code beside them over editing them in place.

Run the self-check after touching either of the solar paths:

```sh
python3 anchors.py
```

It asserts that this repo's sunset equals `solar.py`'s on five cases, including a
high-latitude one, and prints every anchor for a sample station-date.

## The timezone table

`station_timezones.csv` maps each station to an IANA zone (`KEWX,America/Chicago`). It is
needed **only** for `--anchor local_midnight`; every other anchor works without it.

It is generated, not maintained by hand:

```sh
pip install timezonefinder
python3 build_timezones.py
```

`timezonefinder` resolves a coordinate to a zone, but it is a heavy dependency with its
own data files — so the lookup runs once at build time and the result is committed as a
small CSV. At run time the standard library's `zoneinfo` takes the zone name from there
and handles the rest, including daylight saving. A fixed UTC offset per station would
have been wrong for roughly half of every year.

**In a container, `zoneinfo` may find no timezone database at all.** It reads the
system's zone files, and slim base images often ship none — `ZoneInfo("America/Chicago")`
then raises `ZoneInfoNotFoundError`. Either install the OS tzdata package in the image, or
`pip install tzdata`, which `zoneinfo` falls back to. This affects `local_midnight` only;
every other anchor is unaffected, and the tool otherwise needs nothing installed.

Regenerate it when `nexrad_stations.csv` gains a station, or to pick up a newer IANA
release. Review the diff: a zone that changes while a station has not moved is worth
understanding rather than waving through. All 155 stations resolved when it was last
built; a station with a blank zone simply cannot use `local_midnight`.

## Upstream

Volumes come from the Unidata S3 mirror,
`https://unidata-nexrad-level2.s3.amazonaws.com`. The older `noaa-nexrad-level2` bucket
was retired on 2025-09-01, and it never permitted anonymous listing, so it cannot be used
for key discovery.

Archived object names carry a volume suffix that varies by era — `.gz` for older scans,
`_V03`, `_V04`, `_V06`, `_V07` for later ones — and lists of scan names kept by other
software are inconsistent about including it. Keys are therefore resolved by listing the
station-day prefix and matching on the suffix-free stem (`scan_stem`), never by appending
an assumed suffix, which fails silently for whole eras of the record.

## Known gotchas in the data itself

- **Volumes from 2006–2008 report their site position as 0, 0, 0.** Take a radar's
  latitude, longitude and altitude from `nexrad_stations.csv`, never from the file. The
  failure is silent and era-shaped, which is the worst combination.
- **No dual-pol moments (RHOHV, ZDR) before each station's upgrade**, and the upgrade year
  differs by station. Any processing that uses them becomes a function of year.
- **The tree is keyed by UTC day, but people think in local nights.** A Texas sunset
  window sits mostly in the *next* UTC day: local night `20200825` at KEWX is stored under
  `2020/08/26/`. The index carries `ref_date` alongside the key for exactly this reason.

"""fetch_scans.py

Purpose: Fetch NEXRAD Level II volumes into the shared archive, recording who fetched
         what, when, and for which project.
Inputs:  --nights CSV  (station, date[, anchor, from_min, to_min | start_utc, end_utc])
         --keys   TXT  (one S3 key per line; an explicit work list)
         nexrad_stations.csv, station_timezones.csv
Outputs: <archive>/scans/YYYY/MM/DD/STATION/<scan>   the mirror
         <archive>/index/<run_id>.csv                one row per scan this run touched
         <archive>/acquisitions.csv                  one row per run, appended
         <archive>/logs/<run_id>.log                 free text, for debugging

Standard library only. It runs under any Python 3.10+ with nothing installed, which is
the point: the archive is shared, and a tool that needs a conda environment is a tool
other people's students will not use.

Three rules, because the archive is shared
------------------------------------------
NOTHING IS DELETED OR OVERWRITTEN. A scan already on disk is left exactly as it is. To
correct stored content, fetch to a new path; never replace bytes in place.

DOWNLOADS ARE ATOMIC. Bytes land in a `.part` file and are renamed into position only
after the payload matches Content-Length. A killed run therefore leaves no short file
for the next project to mistake for a complete volume - which a plain write does, since
an existence check cannot tell a truncated scan from a whole one.

EVERY RUN IDENTIFIES ITSELF. `--user` and `--project` are required and are written to
`acquisitions.csv` along with the tool's own checksum, so a scan fetched in 2026 can be
traced to the person, the project and the code version that asked for it.

Selection lives with the caller
-------------------------------
This tool knows how to fetch; it does not know which periods are interesting. A project
passes dates with an anchor and offsets, or an explicit list of keys. That boundary is
what lets one archive serve several studies.

THE ANCHOR IS NOT ASSUMED. Emergence work measures from sunset, a dawn study from
sunrise, a convection study from local afternoon, a whole-day pull from UTC midnight.
The default is `utc_midnight` with offsets 0 to 1440 - the UTC day exactly as the archive
files it, needing neither a timezone nor an ephemeris. Anything else is asked for
explicitly, per run or per row, and is recorded per scan. See anchors.py.

Usage
-----
    # a whole UTC day, the default
    python3 fetch_scans.py --nights days.csv --user NETID --project storm_climatology

    # three hours before sunset to one after, this project's window
    python3 fetch_scans.py --nights nights.csv --user NETID --project tabr_drivers \
        --anchor sunset --from-min -180 --to-min 60 --dry-run

    # local clock time: 18:00 through 06:00 at the station
    python3 fetch_scans.py --nights nights.csv --user NETID --project bird_migration \
        --anchor local_midnight --from-min -360 --to-min 360

    python3 fetch_scans.py --keys worklist.txt --user NETID --project tabr_drivers
"""

import argparse
import csv
import hashlib
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date as dt_date
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from anchors import ANCHORS, anchor_utc  # noqa: E402
from nexrad_io import (  # noqa: E402
    UNIDATA_URL,
    list_scan_keys,
    scan_stem,
    scan_time_utc,
)

# No default archive location. An earlier draft derived one from this file's position,
# which silently pointed a fresh clone at a directory nobody meant to write to. The
# archive is named explicitly, or through NEXRAD_L2_ARCHIVE in the environment.
ENV_ARCHIVE = "NEXRAD_L2_ARCHIVE"
DEFAULT_STATIONS = HERE / "nexrad_stations.csv"
DEFAULT_TIMEZONES = HERE / "station_timezones.csv"

# A single row asking for more than a month of volumes is a typo far more often than an
# intention: one mistyped offset turns into weeks of prefix listings aimed at someone
# else's bucket. A genuinely long period is expressed as several rows.
MAX_INTERVAL_DAYS = 31

INDEX_COLUMNS = [
    "run_id", "s3_key", "station", "utc_time", "ref_date", "anchor", "offset_min",
    "bytes", "sha256", "status", "fetched_utc",
]
LEDGER_COLUMNS = [
    "run_id", "utc_started", "utc_finished", "netid", "project", "mode", "selection",
    "selection_sha256", "anchor", "from_min", "to_min", "margin_min", "n_selected", "n_fetched", "n_present",
    "n_missing", "n_failed", "bytes_fetched", "tool", "tool_sha256", "source_commit",
    "host", "notes",
]


def new_run_id(netid: str) -> str:
    """Sortable, unique, and legible at a glance in a directory listing."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{netid}_{uuid.uuid4().hex[:6]}"


def tool_checksum() -> str:
    """Checksum of this tool and the modules it selects and fetches with.

    Covers code only. The station and timezone TABLES also shape what a run selects and
    are not covered here - if either is regenerated, the recorded checksum will not move.

    Recorded per run so a stored scan can be traced to the code that fetched it, without
    depending on the tools directory still holding that version - or on git being
    present, which on a shared drive it is not.
    """
    digest = hashlib.sha256()
    for name in ("fetch_scans.py", "anchors.py", "nexrad_io.py", "solar.py"):
        path = HERE / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def read_stations(path: Path) -> dict[str, tuple[float, float]]:
    with open(path, newline="") as handle:
        return {
            row["station_id"]: (float(row["lat"]), float(row["lon"]))
            for row in csv.DictReader(handle)
        }


def read_timezones(path: Path) -> dict[str, str]:
    """IANA zone per station, generated once with timezonefinder.

    A build-time dependency rather than a run-time one: the lookup is a heavy package,
    the stations do not move, and `zoneinfo` in the standard library handles the rest -
    including DST, which a fixed UTC offset would get wrong for half the year.
    """
    if not path.exists():
        return {}
    with open(path, newline="") as handle:
        return {row["station_id"]: row["tz"] for row in csv.DictReader(handle)}


def read_selection(path: Path) -> list[dict]:
    """Rows to fetch for, with optional per-row anchor and offsets.

    Required: `station` and `date` (`local_date` is accepted as an alias, since existing
    roost-night lists use it). Optional per row: `anchor`, `from_min`, `to_min` to
    override the run's defaults, or `start_utc` / `end_utc` to state an absolute interval
    and bypass anchoring entirely - the escape hatch for a project whose periods are not
    a function of any one instant.
    """
    if not path.exists():
        raise SystemExit(f"No such selection file: {path}")
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(_uncommented(handle)))
    if not rows:
        raise SystemExit(f"{path} holds no rows")
    columns = set(rows[0])
    if "date" not in columns and "local_date" not in columns:
        raise SystemExit(f"{path} needs a `date` (or `local_date`) column")
    if "station" not in columns:
        raise SystemExit(f"{path} needs a `station` column")

    # argparse checks --anchor; nothing checks the column, and a typo there would
    # otherwise surface as a traceback from deep inside the anchor maths.
    bad = sorted({r["anchor"].strip() for r in rows
                  if r.get("anchor") and r["anchor"].strip() not in ANCHORS})
    if bad:
        raise SystemExit(f"{path} has unknown anchor(s) {bad}; expected one of {list(ANCHORS)}")
    return rows


def _uncommented(lines):
    """Drop `#` comment lines, which selection lists often carry as provenance."""
    for line in lines:
        if not line.lstrip().startswith("#"):
            yield line


def _parse_date(text: str) -> dt_date:
    """`20200825` or `2020-08-25`; both turn up in lists people keep by hand."""
    text = text.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise SystemExit(f"unparseable date {text!r}; expected YYYYMMDD or YYYY-MM-DD")


def _parse_utc(text: str) -> datetime:
    """An ISO 8601 instant, with or without the trailing Z."""
    cleaned = text.strip().replace("Z", "+00:00")
    stamp = datetime.fromisoformat(cleaned)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def interval_for(row: dict, date: dt_date, lat: float, lon: float, tz: str,
                 default_anchor: str, default_from: float, default_to: float,
                 margin: float) -> tuple[datetime, datetime, str, datetime | None]:
    """The UTC interval this row asks for, and the anchor it was measured from.

    Returns (low, high, anchor_name, anchor_instant). `anchor_instant` is None for an
    explicit start/end interval, where no anchor exists and per-scan offsets are
    therefore not meaningful.
    """
    if row.get("start_utc") and row.get("end_utc"):
        return _parse_utc(row["start_utc"]), _parse_utc(row["end_utc"]), "explicit", None

    kind = (row.get("anchor") or default_anchor).strip()
    low_min = float(row["from_min"]) if row.get("from_min") else default_from
    high_min = float(row["to_min"]) if row.get("to_min") else default_to

    instant = anchor_utc(kind, date, lat, lon, tz)
    if instant is None:
        return None, None, kind, None
    return (instant + timedelta(minutes=low_min - margin),
            instant + timedelta(minutes=high_min + margin),
            kind, instant)


def check_intervals(rows, default_from: float, default_to: float, margin: float) -> None:
    """Refuse impossible or runaway intervals before a single prefix is listed.

    The span needs no astronomy - it is the offsets alone - so it can be checked up
    front, which is the whole point: the damage a mistyped offset does is measured in
    requests to an upstream bucket, and those happen during listing.
    """
    for n, row in enumerate(rows, start=1):
        where = f"row {n} ({row.get('station', '?')} {row.get('date') or row.get('local_date', '?')})"
        if row.get("start_utc") and row.get("end_utc"):
            span = _parse_utc(row["end_utc"]) - _parse_utc(row["start_utc"])
        else:
            low = float(row["from_min"]) if row.get("from_min") else default_from
            high = float(row["to_min"]) if row.get("to_min") else default_to
            span = timedelta(minutes=(high - low) + 2 * margin)

        if span < timedelta(0):
            raise SystemExit(f"{where}: the interval ends before it starts.")
        if span > timedelta(days=MAX_INTERVAL_DAYS):
            raise SystemExit(
                f"{where}: asks for {span.days} days, over the {MAX_INTERVAL_DAYS}-day "
                "limit. That is usually a mistyped offset; if it is not, split the "
                "period across several rows.")


def check_rows_resolvable(rows, stations, timezones, default_anchor,
                          stations_path, timezones_path, skip_unknown) -> None:
    """Stop before any listing if a row cannot be resolved.

    Two failures this catches, both of which used to surface late and unhelpfully. A
    station that is not in the table selected nothing and the run reported "0 volumes"
    as though that were an answer - the silent kind of wrong this archive exists to
    avoid. And `local_midnight` at a station with no timezone raised a bare ValueError
    from inside the anchor maths, several frames from anything the caller wrote.
    """
    unknown = sorted({r["station"].strip() for r in rows
                      if r["station"].strip() not in stations})
    if unknown:
        message = (f"{len(unknown)} station(s) not in {stations_path}: {unknown}")
        if not skip_unknown:
            raise SystemExit(message + "\nStopping: a mistyped station selects nothing "
                             "and would look like an empty night. Pass "
                             "--skip-unknown-stations to continue past them.")
        logging.warning("%s; continuing as asked.", message)

    # Only rows that will actually use a local anchor need a zone.
    needs_zone = {r["station"].strip() for r in rows
                  if (r.get("anchor") or default_anchor).strip() == "local_midnight"
                  and not (r.get("start_utc") and r.get("end_utc"))}
    missing = sorted(s for s in needs_zone
                     if s in stations and not timezones.get(s))
    if missing:
        raise SystemExit(
            f"anchor local_midnight needs a timezone for {missing}, and {timezones_path} "
            "does not supply one. Regenerate it with build_timezones.py, or choose an "
            "anchor that needs no timezone (utc_midnight, sunrise, sunset, solar_noon, "
            "solar_midnight).")


def select_from_rows(rows, stations, timezones, default_anchor, default_from,
                     default_to, margin) -> list[dict]:
    """Every archived volume inside each row's interval, from S3 prefix listings.

    Every UTC day the interval touches is listed, because a key is filed under its UTC
    day and an interval anchored to local sunset, local midnight or solar midnight
    routinely straddles one.
    """
    listed: dict[str, list[str]] = {}
    selected: list[dict] = []
    seen: set[str] = set()

    for row in rows:
        station = row["station"].strip()
        date_text = (row.get("date") or row.get("local_date") or "").strip()
        date = _parse_date(date_text)
        if station not in stations:
            logging.warning("No coordinates for station %s; skipping %s.", station, date_text)
            continue
        lat, lon = stations[station]

        low, high, anchor_name, instant = interval_for(
            row, date, lat, lon, timezones.get(station, ""),
            default_anchor, default_from, default_to, margin)
        if low is None:
            logging.warning("Anchor %s undefined at %s on %s (polar day or night); skipping.",
                            anchor_name, station, date_text)
            continue

        day = low.date()
        while day <= high.date():
            prefix = f"{day:%Y/%m/%d}/{station}/"
            if prefix not in listed:
                listed[prefix] = list_scan_keys(prefix)
            for key in listed[prefix]:
                scanned = scan_time_utc(key)
                if scanned is None or not low <= scanned <= high or key in seen:
                    continue
                seen.add(key)
                selected.append({
                    "s3_key": key,
                    "station": station,
                    "ref_date": date.strftime("%Y%m%d"),
                    "anchor": anchor_name,
                    "utc_time": scanned.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "offset_min": ("" if instant is None else
                                   round((scanned - instant).total_seconds() / 60.0, 1)),
                })
            day += timedelta(days=1)

    logging.info("Selected %d volumes across %d rows (%d station-days listed).",
                 len(selected), len(rows), len(listed))
    return selected


def select_from_keys(path: Path) -> list[dict]:
    """An explicit work list: the mode a cluster job uses.

    Keys are taken as given and not verified against a listing, so a key that is not in
    the archive is reported per scan rather than up front.
    """
    selected, seen = [], set()
    for line in path.read_text().splitlines():
        key = line.strip()
        if not key or key.startswith("#") or key in seen:
            continue
        seen.add(key)
        scanned = scan_time_utc(key)
        selected.append({
            "s3_key": key,
            "station": scan_stem(key)[:4],
            "ref_date": "",
            "anchor": "keys",
            "utc_time": scanned.strftime("%Y-%m-%dT%H:%M:%SZ") if scanned else "",
            "offset_min": "",
        })
    logging.info("Selected %d volumes from %s.", len(selected), path)
    return selected


def fetch_one(key: str, scans_dir: Path, verify_existing: bool,
              max_retries: int = 3, timeout: int = 120) -> tuple[str, int, str]:
    """Fetch one volume atomically. Returns (status, bytes, sha256).

    A scan already on disk is never re-fetched and never overwritten: the archive is
    shared, and someone else's complete file is not this run's to replace.
    """
    local_path = scans_dir.joinpath(*key.split("/"))
    if local_path.exists() and local_path.stat().st_size > 0:
        size = local_path.stat().st_size
        return "present", size, _sha256(local_path) if verify_existing else ""

    local_path.parent.mkdir(parents=True, exist_ok=True)
    part = local_path.with_name(f"{local_path.name}.part-{os.getpid()}")
    url = f"{UNIDATA_URL}/{key}"

    for attempt in range(1, max_retries + 1):
        try:
            digest, written = hashlib.sha256(), 0
            with urllib.request.urlopen(url, timeout=timeout) as response:
                expected = int(response.headers.get("Content-Length", 0))
                with open(part, "wb") as handle:
                    while chunk := response.read(1 << 20):
                        handle.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
            if expected and written != expected:
                raise OSError(f"truncated: got {written} of {expected} bytes")
            # Atomic within the directory: no reader ever sees a partial scan.
            os.replace(part, local_path)
            return "fetched", written, digest.hexdigest()
        except urllib.error.HTTPError as exc:
            part.unlink(missing_ok=True)
            if exc.code == 404:
                return "not_in_archive", 0, ""
            if attempt == max_retries:
                logging.error("%s failed after %d attempts: %s", key, max_retries, exc)
                return "failed", 0, ""
            time.sleep(2 ** attempt)
        except Exception as exc:  # noqa: BLE001 - the status is recorded, the run goes on
            part.unlink(missing_ok=True)
            if attempt == max_retries:
                logging.error("%s failed after %d attempts: %s", key, max_retries, exc)
                return "failed", 0, ""
            time.sleep(2 ** attempt)

    return "failed", 0, ""


def store_selection(path: Path, archive: Path) -> str:
    """Keep the run's own selection file, addressed by its content. Returns the hash.

    The ledger records which file a run was given, but a path is not a record: the file
    is edited, and two rows citing `nights.csv` then describe different requests. Storing
    the bytes makes the request itself recoverable - and the request is the only place a
    row that resolved to NO volumes survives, since the index lists what was touched. A
    night with nothing in its window is a data gap worth being able to find again.

    Content-addressed rather than one copy per run: the same list fetched fifty times is
    stored once, and a name that is a hash of the bytes cannot be updated in place, so
    the archive's no-overwrite rule holds by construction rather than by discipline.
    """
    digest = _sha256(path)
    store = archive / "selections"
    store.mkdir(parents=True, exist_ok=True)
    stored = store / f"{digest[:16]}{path.suffix or '.txt'}"
    if not stored.exists():
        stored.write_bytes(path.read_bytes())
    return digest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch NEXRAD Level II volumes into the shared archive.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dates", type=Path,
                        help="CSV of station,date[,anchor,from_min,to_min|start_utc,end_utc]")
    source.add_argument("--keys", type=Path, help="text file of S3 keys, one per line")
    parser.add_argument("--user", required=True, help="NetID of the person running this")
    parser.add_argument("--project", required=True,
                        help="short project name, e.g. tabr_drivers")
    parser.add_argument("--anchor", choices=ANCHORS, default="utc_midnight",
                        help="instant the offsets are measured from (default: "
                             "utc_midnight, i.e. the UTC day as the archive files it)")
    parser.add_argument("--from-min", type=float, default=0.0,
                        help="minutes from the anchor for the start (default 0)")
    parser.add_argument("--to-min", type=float, default=1440.0,
                        help="minutes from the anchor for the end (default 1440)")
    parser.add_argument("--margin", type=float, default=0.0,
                        help="extra minutes either side, so an interval edge is not a cliff")
    parser.add_argument("--archive", type=Path, default=None,
                        help=f"archive root to write into; required unless "
                             f"${ENV_ARCHIVE} is set in the environment")
    parser.add_argument("--stations", type=Path, default=DEFAULT_STATIONS)
    parser.add_argument("--timezones", type=Path, default=DEFAULT_TIMEZONES,
                        help="station_id,tz table; needed only for --anchor local_midnight")
    parser.add_argument("--source-commit", default="",
                        help="git SHA of the project that requested this run, if any")
    parser.add_argument("--notes", default="", help="free text for the ledger")
    parser.add_argument("--verify-existing", action="store_true",
                        help="record a checksum for scans already on disk, so a later "
                             "integrity pass has a baseline; reads every such file, so "
                             "off by default")
    parser.add_argument("--skip-unknown-stations", action="store_true",
                        help="warn and continue past stations missing from the station "
                             "table, instead of stopping. Off by default: a typo that "
                             "silently fetches nothing is worse than a run that stops")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve and report, writing nothing at all")
    args = parser.parse_args()

    if args.archive is None:
        from_env = os.environ.get(ENV_ARCHIVE)
        if not from_env:
            raise SystemExit(
                f"No archive given. Pass --archive /path/to/nexrad_l2_data, or set "
                f"{ENV_ARCHIVE} in your environment. There is deliberately no default: "
                "guessing where to put several TB of radar data is not a favour.")
        args.archive = Path(from_env)

    run_id = new_run_id(args.user)
    scans_dir = args.archive / "scans"
    started = datetime.now(timezone.utc)

    handlers = [logging.StreamHandler(sys.stdout)]
    if not args.dry_run:
        (args.archive / "logs").mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.archive / "logs" / f"{run_id}.log"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.info("run_id %s  user %s  project %s", run_id, args.user, args.project)
    logging.info("archive %s", args.archive)

    if not args.dry_run:
        check_ledger_schema(args.archive / "acquisitions.csv")

    stations = read_stations(args.stations)
    timezones = read_timezones(args.timezones)
    if args.dates:
        rows = read_selection(args.dates)
        check_intervals(rows, args.from_min, args.to_min, args.margin)
        check_rows_resolvable(rows, stations, timezones, args.anchor,
                              args.stations, args.timezones, args.skip_unknown_stations)
        selected = select_from_rows(rows, stations, timezones,
                                    args.anchor, args.from_min, args.to_min, args.margin)
        mode, selection = "dates", str(args.dates)
    else:
        selected = select_from_keys(args.keys)
        mode, selection = "keys", str(args.keys)

    if args.dry_run:
        on_disk = sum(scans_dir.joinpath(*r["s3_key"].split("/")).exists() for r in selected)
        logging.info("Dry run: %d selected, %d already in the archive, %d to fetch.",
                     len(selected), on_disk, len(selected) - on_disk)
        logging.info("Dry run: nothing written, no ledger row recorded.")
        return

    # Stored before a single volume is fetched, so an interrupted run still leaves its
    # request on record.
    selection_sha = store_selection(args.dates or args.keys, args.archive)

    (args.archive / "index").mkdir(parents=True, exist_ok=True)
    index_path = args.archive / "index" / f"{run_id}.csv"
    counts = {"fetched": 0, "present": 0, "not_in_archive": 0, "failed": 0}
    bytes_fetched = 0

    # One index file per run, never appended to by anyone else: two people fetching at
    # once cannot interleave rows, and no existing file is ever modified.
    with open(index_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_COLUMNS)
        writer.writeheader()
        try:
            for n, row in enumerate(selected, start=1):
                status, size, digest = fetch_one(row["s3_key"], scans_dir,
                                                 args.verify_existing)
                counts[status] += 1
                if status == "fetched":
                    bytes_fetched += size
                writer.writerow({
                    "run_id": run_id, "s3_key": row["s3_key"], "station": row["station"],
                    "utc_time": row["utc_time"], "ref_date": row["ref_date"],
                    "anchor": row["anchor"], "offset_min": row["offset_min"],
                    "bytes": size or "",
                    "sha256": digest, "status": status,
                    "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
                if n % 100 == 0:
                    handle.flush()
                    logging.info("%d/%d  fetched %d, present %d, missing %d, failed %d",
                                 n, len(selected), counts["fetched"], counts["present"],
                                 counts["not_in_archive"], counts["failed"])
        except KeyboardInterrupt:
            logging.warning("Interrupted; recording what was fetched.")
            raise
        finally:
            handle.flush()

    finished = datetime.now(timezone.utc)
    ledger_row = {
        "run_id": run_id,
        "utc_started": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "utc_finished": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "netid": args.user, "project": args.project, "mode": mode, "selection": selection,
        "selection_sha256": selection_sha,
        "anchor": args.anchor if mode == "dates" else "keys",
        "from_min": args.from_min if mode == "dates" else "",
        "to_min": args.to_min if mode == "dates" else "",
        "margin_min": args.margin if mode == "dates" else "",
        "n_selected": len(selected), "n_fetched": counts["fetched"],
        "n_present": counts["present"], "n_missing": counts["not_in_archive"],
        "n_failed": counts["failed"], "bytes_fetched": bytes_fetched,
        "tool": "fetch_scans.py", "tool_sha256": tool_checksum(),
        "source_commit": args.source_commit, "host": socket.gethostname(),
        "notes": args.notes,
    }
    append_ledger(args.archive / "acquisitions.csv", ledger_row)

    logging.info("Fetched %d (%.2f GB), already present %d, not in archive %d, failed %d.",
                 counts["fetched"], bytes_fetched / 1e9, counts["present"],
                 counts["not_in_archive"], counts["failed"])
    logging.info("Index %s", index_path)


def check_ledger_schema(path: Path) -> None:
    """Refuse to append rows a later tool version would misread.

    Checked BEFORE anything is fetched, so a schema change fails in the first second
    rather than after an hour of downloading. A ledger whose header is from one version
    and whose rows are from another is not repairable without editing it, and editing an
    existing shared file is exactly what this archive does not do: the remedy is to
    retire the old ledger under a new name by hand and let a fresh one be created.
    """
    if not path.exists():
        return
    with open(path, newline="") as handle:
        header = next(csv.reader(handle), [])
    if header != LEDGER_COLUMNS:
        raise SystemExit(
            f"{path} was written by a different version of this tool.\n"
            f"  ledger columns: {header}\n"
            f"  tool columns:   {LEDGER_COLUMNS}\n"
            "Refusing to append: mixing two schemas in one append-only file makes it "
            "unreadable, and nothing existing is ever rewritten here. Rename the old "
            "ledger by hand, then re-run to start a fresh one.")


def append_ledger(path: Path, row: dict) -> None:
    """Append one row, writing the header only when the ledger is created.

    Append-only by design: a run adds its own line and never rewrites another's. One
    line per run keeps the concurrent-append window small, which matters because this
    file lives on a network share two people may write at the same moment.
    """
    check_ledger_schema(path)
    exists = path.exists()
    with open(path, "a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LEDGER_COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


if __name__ == "__main__":
    main()

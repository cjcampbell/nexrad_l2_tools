"""seed_index.py

Give an archive that predates this tool the records it would have had.

A tree of volumes with no index and no ledger is the thing SCHEMAS.md says an archive
must not be: nobody can say who fetched them, for what, or whether they are whole. This
walks an existing `scans/` tree and writes the records after the fact - one ledger row
per seed run, one index shard, and a copy of whatever list is standing in for the
request.

It never downloads, never deletes, and never overwrites. Volumes are read only to size
them (and to checksum them, with --checksum).

Two runs are written, not one, because the two groups are not equally knowable:

  1. Volumes an external manifest accounts for. Their recorded byte count travels with
     them, and the manifest is stored as the run's selection.
  2. Volumes on disk that the manifest does not mention. These get their own run, their
     own generated key list as its selection, and a ledger note saying plainly that
     their provenance is unknown. Folding them into the first run would assert a
     provenance they do not have.

Usage
-----
    python3 seed_index.py --archive ~/NEXRAD_l2_data --user NETID --project my_project \
        --manifest /path/to/download_manifest.csv --dry-run
"""

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fetch_scans import (  # noqa: E402
    INDEX_COLUMNS,
    LEDGER_COLUMNS,
    _sha256,
    append_ledger,
    check_ledger_schema,
    store_selection,
)
from nexrad_io import scan_stem, scan_time_utc  # noqa: E402

# Finder and friends leave these in shared trees; they are not volumes.
JUNK = {".DS_Store", "Thumbs.db", ".directory"}


def walk_scans(scans_dir: Path) -> list[str]:
    """Every volume in the tree, as archive-relative keys."""
    return sorted(
        str(p.relative_to(scans_dir)) for p in scans_dir.rglob("*")
        if p.is_file() and p.name not in JUNK and ".part-" not in p.name
    )


def read_manifest(path: Path) -> dict[str, dict]:
    """Keyed by s3_key. Only `bytes` and `local_date` are used, both optional."""
    with open(path, newline="") as handle:
        return {row["s3_key"]: row for row in csv.DictReader(handle) if row.get("s3_key")}


def index_row(run_id: str, key: str, scans_dir: Path, recorded: dict | None,
              checksum: bool) -> dict:
    path = scans_dir.joinpath(*key.split("/"))
    scanned = scan_time_utc(key)
    return {
        "run_id": run_id,
        "s3_key": key,
        "station": scan_stem(key)[:4],
        "utc_time": scanned.strftime("%Y-%m-%dT%H:%M:%SZ") if scanned else "",
        # The date the volume was fetched FOR, which only the manifest knows.
        "ref_date": (recorded or {}).get("local_date", ""),
        "anchor": "seed",
        "offset_min": "",
        "bytes": path.stat().st_size,
        "sha256": _sha256(path) if checksum else "",
        "status": "present",
        "fetched_utc": "",  # unknown: these were fetched before any record was kept
    }


def write_seed_run(archive: Path, scans_dir: Path, keys: list[str], selection: Path,
                   user: str, project: str, notes: str, checksum: bool,
                   manifest: dict, suffix: str) -> str:
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{user}_{suffix}"
    started = datetime.now(timezone.utc)

    selection_sha = store_selection(selection, archive)
    (archive / "index").mkdir(parents=True, exist_ok=True)
    total = 0
    with open(archive / "index" / f"{run_id}.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_COLUMNS)
        writer.writeheader()
        for key in keys:
            row = index_row(run_id, key, scans_dir, manifest.get(key), checksum)
            total += int(row["bytes"])
            writer.writerow(row)

    append_ledger(archive / "acquisitions.csv", {
        "run_id": run_id,
        "utc_started": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "utc_finished": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "netid": user, "project": project, "mode": "seed", "selection": str(selection),
        "selection_sha256": selection_sha, "anchor": "seed", "from_min": "", "to_min": "",
        "margin_min": "", "n_selected": len(keys), "n_fetched": 0, "n_present": len(keys),
        "n_missing": 0, "n_failed": 0, "bytes_fetched": 0,
        "tool": "seed_index.py", "tool_sha256": "", "source_commit": "",
        "host": "", "notes": notes,
    })
    print(f"  {run_id}: {len(keys):,} volumes, {total / 1e9:.2f} GB")
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[2])
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--manifest", type=Path,
                        help="CSV with an s3_key column, and optionally bytes/local_date")
    parser.add_argument("--checksum", action="store_true",
                        help="sha256 every volume, giving later integrity checks a "
                             "baseline; reads the whole archive")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    scans_dir = args.archive / "scans"
    if not scans_dir.is_dir():
        raise SystemExit(f"No scans directory under {args.archive}")
    if (args.archive / "acquisitions.csv").exists():
        check_ledger_schema(args.archive / "acquisitions.csv")

    keys = walk_scans(scans_dir)
    manifest = read_manifest(args.manifest) if args.manifest else {}
    known = [k for k in keys if k in manifest]
    unknown = [k for k in keys if k not in manifest]

    print(f"{len(keys):,} volumes under {scans_dir}")
    print(f"  {len(known):,} accounted for by {args.manifest}")
    print(f"  {len(unknown):,} with no record of where they came from")
    if args.dry_run:
        print("Dry run: nothing written.")
        return

    if known:
        write_seed_run(args.archive, scans_dir, known, args.manifest, args.user,
                       args.project,
                       f"Seeded from {args.manifest.name}. Fetched before this tool "
                       "existed; fetch times and checksums were never recorded.",
                       args.checksum, manifest, "seed")
    if unknown:
        # The list is generated here, so it is written where it can be stored as the
        # run's selection - the same treatment any other request gets.
        listing = args.archive / "selections" / "_unrecorded.txt"
        listing.parent.mkdir(parents=True, exist_ok=True)
        listing.write_text("\n".join(unknown) + "\n")
        write_seed_run(args.archive, scans_dir, unknown, listing, args.user,
                       args.project,
                       "PROVENANCE UNKNOWN: present on disk, absent from the manifest. "
                       "Who fetched these, when, and for what is not recorded anywhere.",
                       args.checksum, manifest, "unrecorded")
        listing.unlink()  # it lives on content-addressed under selections/


if __name__ == "__main__":
    main()

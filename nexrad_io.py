"""NEXRAD Level II discovery and download.

Vendored deliberately. The earlier processing project has an equivalent module
(`../TABR radar data/utils/nexrad_io.py`) and this duplicates it, so that the
acquisition step is reproducible from this repository alone when it is released
alongside the manuscript.

This is now this project's module and diverges from that one on purpose (see below).
Do not sync changes across blindly in either direction: adopting that module's key
construction would reintroduce the failure described below. Read it when debugging a
discrepancy, then decide.

Endpoint: the Unidata S3 mirror. The `noaa-nexrad-level2` bucket returns AccessDenied
for anonymous ListObjectsV2, so it is not usable for discovery.

The divergence, and the reason this module is not a thin copy: the `filename` column
of the screened labels and the archived object names agree on neither suffix nor
convention. Archived objects always carry a suffix, and which one varies by era: a
2007 scan is `KDFX20070104_003831.gz`, a modern one `KDFX20240101_003831_V06`. The
label column is inconsistent with itself - measured across the 80 station-year files,
2006-2007 carry no suffix at all, 2008 is about half and half, and 2009 onward always
carries one, drawn from `_V03`, `_V04`, `_V06` and `_V07`. Constructing a key by
appending an assumed suffix therefore fails silently on part of the record whichever
suffix is assumed. Keys are resolved here by listing the station-date prefix, and
every join between labels and scans goes through `scan_stem()`.
"""

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

UNIDATA_URL = "https://unidata-nexrad-level2.s3.amazonaws.com"
_S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def scan_prefix(filename: str) -> str:
    """S3 prefix for the station-date a scan filename belongs to.

    `KDFX20070104_003831` -> `2007/01/04/KDFX/`
    """
    station, date = filename[:4], filename[4:12]
    return f"{date[:4]}/{date[4:6]}/{date[6:8]}/{station}/"


def scan_stem(name: str) -> str:
    """Suffix-free `STATIONYYYYMMDD_HHMMSS` stem of a scan key, filename or label.

    The one safe join key between the screened labels and the archive. Both sides
    carry suffixes inconsistently - see the module docstring - so joining on the raw
    strings silently matches nothing for whole eras:

    `2011/03/30/KEWX/KEWX20110330_000035_V03.gz` -> `KEWX20110330_000035`
    `KEWX20110330_000035_V03`                    -> `KEWX20110330_000035`
    `KDFX20070104_003831`                        -> `KDFX20070104_003831`
    """
    return name.rsplit("/", 1)[-1][:19]


def scan_time_utc(key: str) -> "datetime | None":
    """UTC start time encoded in a scan key or filename.

    `2020/08/25/KEWX/KEWX20200825_233556_V06` -> 2020-08-25 23:35:56 UTC.

    Returns None for objects that are not volume scans. The archive carries `_MDM`
    metadata objects alongside the volumes, and listing a prefix returns both.
    """
    base = key.rsplit("/", 1)[-1]
    if base.endswith("_MDM"):
        return None
    try:
        return datetime.strptime(base[4:19], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def list_scan_keys(prefix: str, timeout: int = 30) -> list[str]:
    """List every object key under an S3 prefix, following continuation tokens."""
    keys: list[str] = []
    params = {"list-type": "2", "prefix": prefix}
    while True:
        query = urllib.parse.urlencode(params)
        with urllib.request.urlopen(f"{UNIDATA_URL}/?{query}", timeout=timeout) as response:
            root = ET.fromstring(response.read())
        keys.extend(c.find(f"{{{_S3_NS}}}Key").text for c in root.iter(f"{{{_S3_NS}}}Contents"))

        truncated = root.find(f"{{{_S3_NS}}}IsTruncated")
        if truncated is None or truncated.text.lower() != "true":
            break
        params["continuation-token"] = root.find(f"{{{_S3_NS}}}NextContinuationToken").text
    return keys


def resolve_scan_key(filename: str, available_keys: list[str]) -> str | None:
    """Match a suffix-free scan filename to its actual archived key.

    Returns None when the scan is not in the archive, which is a real outcome rather
    than an error: the screened data occasionally references scans that were later
    withdrawn. The caller records the miss in the manifest.
    """
    stem = filename.split(".")[0]
    matches = [k for k in available_keys if k.rsplit("/", 1)[-1].startswith(stem)]
    if not matches:
        return None
    # Deterministic when an era transition leaves two objects for one stem.
    return sorted(matches)[0]


def download_scan(
    s3_key: str,
    local_dir: Path,
    skip_existing: bool = True,
    max_retries: int = 3,
) -> tuple[Path, int]:
    """Fetch one scan, mirroring the key structure under `local_dir`.

    Returns the local path and its size in bytes. Retries with exponential backoff,
    and verifies the payload against Content-Length so a truncated read is not
    written to disk as if it were complete.
    """
    local_path = local_dir.joinpath(*s3_key.split("/"))
    local_path.parent.mkdir(parents=True, exist_ok=True)

    if skip_existing and local_path.exists() and local_path.stat().st_size > 0:
        return local_path, local_path.stat().st_size

    url = f"{UNIDATA_URL}/{s3_key}"
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                expected = int(response.headers.get("Content-Length", 0))
                payload = response.read()
            if expected and len(payload) != expected:
                raise RuntimeError(f"Truncated: got {len(payload)} of {expected} bytes")
            local_path.write_bytes(payload)
            return local_path, len(payload)
        except Exception as exc:
            if attempt == max_retries:
                raise RuntimeError(
                    f"Failed to download {s3_key} after {max_retries} attempts: {exc}"
                ) from exc
            time.sleep(2 ** attempt)

    raise AssertionError("unreachable")

# Archive record-keeping

Two records, both append-only. Nothing in either is ever edited or deleted; a correction
is a new row, and a re-fetch is a new run.

## Choosing a period: the anchor

The archive has no house opinion about when a day starts. Every selection is an
**anchor** plus offsets in minutes, so a project states its own reference rather than
inheriting someone else's.

| anchor | the instant offsets are measured from | needs |
|---|---|---|
| `utc_midnight` | 00:00 UTC on the date. **The default** | nothing |
| `local_midnight` | 00:00 civil time at the station, DST-aware | `station_timezones.csv` |
| `sunrise`, `sunset` | refraction-corrected horizon crossing (−0.833°) | station coordinates |
| `solar_noon` | solar transit — not 12:00 local | station coordinates |
| `solar_midnight` | antitransit, defined as solar noon + 12 h | station coordinates |

`--anchor utc_midnight --from-min 0 --to-min 1440` is the default and the safe one: the
UTC day exactly as the archive files it, with no timezone and no ephemeris involved.

Examples of the same idea:

```
--anchor sunset         --from-min -180 --to-min   60    # TABR emergence window
--anchor local_midnight --from-min -360 --to-min  360    # 18:00 to 06:00 local
--anchor solar_noon     --from-min -120 --to-min  120    # afternoon convection
```

**Both endpoints are inclusive.** With the whole-day default, a volume starting exactly
at the next UTC midnight therefore belongs to both days. It costs an index row, never a
duplicate file, because the mirror is keyed by path.

## Selection file (`--dates`)

| column | required | meaning |
|---|---|---|
| `station` | yes | 4-letter station id |
| `date` | yes | `YYYYMMDD` or `YYYY-MM-DD`; `local_date` accepted as an alias |
| `anchor`, `from_min`, `to_min` | no | override the run's defaults for this row |
| `start_utc`, `end_utc` | no | absolute ISO 8601 interval; bypasses anchoring entirely |

Per-row overrides are how a seasonal rule stays with the project: this pipeline opens
three hours before sunset in summer and two otherwise, so stage 1 emits the window it
computed and the archive simply honours it.

`#` comment lines are skipped, so provenance notes can live in the file.

## `acquisitions.csv` — one row per run

| column | meaning |
|---|---|
| `run_id` | `YYYYMMDDTHHMMSSZ_<netid>_<6 hex>`; names the index shard and the log |
| `utc_started`, `utc_finished` | run bounds |
| `netid`, `project` | who, and what for — both required |
| `mode` | `dates` or `keys` |
| `selection` | path of the file the run was given |
| `anchor`, `from_min`, `to_min`, `margin_min` | the period rule; `anchor` is `keys` in key mode |
| `n_selected` | volumes the selection resolved to |
| `n_fetched` | newly downloaded |
| `n_present` | already on disk, left untouched |
| `n_missing` | not in the upstream archive — a real outcome, not an error |
| `n_failed` | failed after retries |
| `bytes_fetched` | new bytes only |
| `tool`, `tool_sha256` | the code that ran, checksummed over the tool and its vendored modules |
| `source_commit` | git SHA of the requesting project, when passed |
| `host`, `notes` | where it ran; free text |

**Schema drift is refused, not merged.** Before fetching, the tool compares the ledger's
header to its own columns and stops if they differ — a ledger with one version's header
and another's rows is unreadable, and repairing it would mean editing a shared file. The
remedy is to rename the old ledger by hand and let a fresh one be created.

## `index/<run_id>.csv` — one row per volume touched

| column | meaning |
|---|---|
| `run_id` | links back to the ledger |
| `s3_key` | `YYYY/MM/DD/STATION/<scan>`; also the path under `scans/` |
| `station` | 4-letter station id |
| `utc_time` | volume start, UTC |
| `ref_date` | the date the row was selected *for*; blank in `keys` mode |
| `anchor` | which anchor produced it (`explicit` for a start/end interval, `keys` for a key list) |
| `offset_min` | minutes from the anchor instant; blank where there is no anchor |
| `bytes`, `sha256` | size and checksum; `sha256` blank for `present` rows unless `--verify-existing` |
| `status` | `fetched`, `present`, `not_in_archive`, `failed` |
| `fetched_utc` | when the row was written |

**One file per run, never appended to by anyone else.** Two people fetching at the same
moment cannot interleave rows, and no existing file is modified. The cost is that the
index is a set of shards — read them with a glob.

## How to ask the archive a question

**"Is this scan here?"** Check the filesystem, not the index. The tree mirrors the S3 key
structure, so the path *is* the lookup, and it stays right even for scans that arrived
some other way. The index records provenance; it is not the existence oracle.

**"Who has already pulled KEWX summer 2015?"** Glob the index shards, or grep the ledger
by project.

**"What did run X do?"** `logs/<run_id>.log` is the account; the ledger row is the summary.

A rollup (`index/rollup/scans_<timestamp>.csv`) is worth adding once globbing gets slow.
Write a new dated file each time rather than replacing one, so it obeys the same rule.

## One trap worth repeating

**`ref_date` and the key's date are different days.** The tree is keyed by UTC day; a
Texas sunset window sits mostly in the *next* UTC day. In testing, local night `20200825`
stored under `2020/08/26/KEWX/`. Anything that groups by the path's date rather than by
`ref_date` will silently split every night in two.

# Theme collection date policy and historical audit (2026-11-11)

Collection time (`collected_at`, timezone included) and market date are independent.
The collector verifies the latest positive-volume daily-bar date of Samsung Electronics
and SK Hynix using KIS; both must agree. This requires KIS credentials even with warm state.
Weekends and exchange holidays use the last actual session rather than calendar arithmetic.
08:00–16:09 KST is conservatively blocked, including holidays and special opening days.
A requested date is an assertion, not permission to backfill live Naver rankings.
Original Actions run creation time is checked on reruns to reject a different live session.
The workflow no longer restores a wall-clock-date cache over committed data state.

Duplicate dates are skipped before ranking retrieval; future-dated state fails closed.
Daily files cannot be overwritten. Unchanged numerical rankings no longer imply a holiday.
Existing concurrency serializes data writers; a rejected git push must be investigated,
not force-pushed. Check logs for collected_at and market_date.

## Limits

Naver theme aggregates expose no validated session timestamp in the current provider.
KIS reference dates verify exchange sessions, not the timestamp of every Naver aggregate.
A stale response shared by both reference stocks or a stale Naver response cannot be
fully excluded. Provider errors, disagreement and unsafe collection windows fail closed.
Historical live backfill is prohibited; reliable repair requires preserved snapshots.
Future-dated legacy state deliberately blocks collection until reviewed and corrected.

## Read-only historical audit

Examined all commits affecting `daily` on data branch through a2b4e9a, plus current daily
payloads. Times below are commit times (KST), not independently verified job start times.
The reported run 42845295044 independently confirms a 00:31:54 start on November 11.
No existing data has been changed.

| Source commit | Commit time KST | Stored date | Likely source session | Proposed treatment |
|---|---|---|---|---|
| 498600a | Oct 13 01:41 | Oct 13 | Oct 12 | Recover this historical payload as Oct 12; preserve later Oct 13 payload from ca27c91 |
| 60152d3 | Nov 3 01:59 | Nov 3 | Nov 2 | Move payload to Nov 2 and change market_date |
| 38cf313 | Nov 4 00:02 | Nov 4 | Nov 3 | Move payload to Nov 3 and change market_date |
| db78d22 | Nov 5 00:22 | Nov 5 | Nov 4 | Move payload to Nov 4 and change market_date |
| 17c7707 | Nov 10 10:00 | Nov 10 | Intraday or stale; not verified | Quarantine as unverified, do not treat as confirmed close |
| a2b4e9a | Nov 11 00:32 | Nov 11 | Nov 10 | Replace unverified Nov 10 with this preserved close snapshot; remove mislabeled Nov 11 |

The October 7 14:18 initial collection and 14:59 historical backfill also predate close;
subsequent Oct 7 collections exist. Historical synthetic bootstrap values cannot be
certified as official theme OHLC. They should not be silently renamed.

## Approval-required repair scope

Only after explicit approval: stage all payloads before moves to avoid filename collisions.
Repair daily filenames and market_date for the rows above; preserve the unverified Nov 10
payload in an audit archive. Recover the Oct 12 payload from git, not the overwritten current
Oct 13 file. Rebuild affected `state/theme-history.json` point dates and cumulative closes
from the last trustworthy predecessor; renaming alone leaves compounded returns incorrect.
Reconcile affected `memberships/*.json` effective_date and catalog/themes.json updated_at
using their source commits. Report exact numeric diffs before committing this broader repair.
Provider session dates for historical rows remain inferred until original logs/source data
are checked; this table is a proposed repair, not a certified historical reconstruction.

Validation: `PYTHONPATH=src python -m unittest discover -s tests -q`.

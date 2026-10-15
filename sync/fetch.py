"""Pull data from TrainingPeaks into a local cache (read-only: this script never writes to TrainingPeaks).

Cache layout (sync/cache/):
  workouts.json      {workoutId: raw TP workout}            refreshed every run over the full window
  files/<id>.json.gz parsed device file (streams, laps, RR) fetched once per workout
  files_meta.json    {workoutId: {"status", "file_id", "modified"}}
  metrics.json       raw consolidated timed metrics (HRV, resting HR, sleep, weight)
  events.json        races / events with priority
  profile.json       athlete id + name (no credentials)

Auth: TP_AUTH_COOKIE env var (handled by tp-mcp). Optional TP_ATHLETE for coach accounts.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import os
import sys
from pathlib import Path

from tp_mcp.client import TPClient
from tp_mcp.client.context import athlete_override

sys.path.insert(0, str(Path(__file__).parent))
from fitparse_util import parse_activity_file  # noqa: E402

WINDOW = 90               # TP list endpoints accept <= 90 days
MAX_DOWNLOADS = int(os.environ.get("TP_MAX_DOWNLOADS", "1500"))


class SyncAuthError(RuntimeError):
    pass


def _windows(start: dt.date, end: dt.date, size: int = WINDOW):
    cur = start
    while cur <= end:
        nxt = min(cur + dt.timedelta(days=size - 1), end)
        yield cur, nxt
        cur = nxt + dt.timedelta(days=1)


def _check(resp, what: str, warnings: list):
    if resp.success:
        return True
    code = resp.error_code.value if resp.error_code else "API_ERROR"
    if code in ("AUTH_EXPIRED", "AUTH_INVALID"):
        raise SyncAuthError(f"TrainingPeaks rejected the session while fetching {what} ({code}). "
                            "Refresh the TP_AUTH_COOKIE secret.")
    warnings.append(f"{what}: {code} {resp.message}")
    return False


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


async def fetch(cache: Path, start: dt.date, today: dt.date, ahead_days: int = 120) -> dict:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "files").mkdir(exist_ok=True)
    warnings: list[str] = []
    athlete = os.environ.get("TP_ATHLETE") or None
    if athlete:
        athlete_override.set(athlete)

    async with TPClient() as client:
        # Step 1: is the cookie accepted at all?
        user = await client._get_user_data()
        if not user:
            raise SyncAuthError("COOKIE REJECTED: TrainingPeaks would not return the account profile. "
                                "Re-copy the full Production_tpAuth cookie value (logged in as the coach) into the "
                                "TP_AUTH_COOKIE secret. Do not include the cookie name or quotes.")
        roster = [a for a in (user.get("athletes") or []) if isinstance(a, dict)]
        # Step 2: does TP_ATHLETE match someone on this account?
        try:
            aid = await client.ensure_athlete_id()
        except ValueError as e:  # ambiguous athlete name on a coach account
            raise SyncAuthError(f"ATHLETE AMBIGUOUS: {e} Set the TP_ATHLETE variable to the athlete ID.")
        if not aid:
            # initials + IDs only, so the (publicly viewable) log does not carry full names
            ids = ", ".join(f"{(a.get('firstName') or '?')[:1]}.{(a.get('lastName') or '?')[:1]}.={a.get('athleteId')}"
                            for a in roster[:40]) or "none"
            raise SyncAuthError(f"ATHLETE NOT FOUND: cookie works, but TP_ATHLETE='{athlete}' matched none of the "
                                f"{len(roster)} athletes on this account ({ids}). Set the TP_ATHLETE repository "
                                "variable to the right athlete ID.")
        who = next((a for a in roster if a.get("athleteId") == aid), user)
        (cache / "profile.json").write_text(json.dumps({"athlete_id": aid,
                                                         "name": " ".join(x for x in [who.get("firstName"), who.get("lastName")] if x)}))

        # ---- workouts (completed + planned), full window every run so edits/deletions propagate
        end = today + dt.timedelta(days=ahead_days)
        workouts: dict[str, dict] = {}
        for a, b in _windows(start, end):
            r = await client.get(f"/fitness/v6/athletes/{aid}/workouts/{a.isoformat()}/{b.isoformat()}")
            if _check(r, f"workouts {a}..{b}", warnings) and isinstance(r.data, list):
                for w in r.data:
                    if isinstance(w, dict) and w.get("workoutId") is not None:
                        workouts[str(w["workoutId"])] = w
        if not workouts and warnings:
            raise RuntimeError("No workouts could be fetched: " + "; ".join(warnings[:3]))
        (cache / "workouts.json").write_text(json.dumps(workouts))

        # ---- device files for completed workouts not yet cached (or modified since)
        meta = _load(cache / "files_meta.json", {})
        todo = []
        for wid, w in workouts.items():
            done = (w.get("totalTime") is not None and w.get("workoutTypeValueId") != 7
                    and str(w.get("workoutDay", ""))[:10] <= today.isoformat())
            if not done:
                continue
            mod = w.get("lastModifiedDate") or ""
            m = meta.get(wid)
            if m is None or (m.get("status") == "ok" and mod and m.get("modified") and mod > m["modified"]
                             and str(w.get("workoutDay", ""))[:10] >= (today - dt.timedelta(days=21)).isoformat()):
                todo.append(wid)
            elif m.get("status") == "error" and m.get("tries", 0) < 3:
                todo.append(wid)
        todo.sort(key=lambda k: str(workouts[k].get("workoutDay")), reverse=True)  # newest first
        downloaded = 0
        for wid in todo[:MAX_DOWNLOADS]:
            prev = meta.get(wid, {})
            d = await client.get(f"/fitness/v6/athletes/{aid}/workouts/{wid}/details")
            if not _check(d, f"details {wid}", warnings):
                meta[wid] = {"status": "error", "tries": prev.get("tries", 0) + 1}
                continue
            infos = (d.data or {}).get("workoutDeviceFileInfos") if isinstance(d.data, dict) else None
            infos = [i for i in (infos or []) if isinstance(i, dict) and i.get("fileId") is not None]
            if not infos:
                meta[wid] = {"status": "none", "modified": workouts[wid].get("lastModifiedDate")}
                continue
            fid = str(infos[0]["fileId"])
            raw = await client.get_raw(f"/fitness/v6/athletes/{aid}/workouts/{wid}/rawfiledata/{fid}")
            if raw.is_error:
                _check(raw, f"file {wid}/{fid}", warnings)
                meta[wid] = {"status": "error", "tries": prev.get("tries", 0) + 1}
                continue
            try:
                parsed = parse_activity_file(raw.content)
                with gzip.open(cache / "files" / f"{wid}.json.gz", "wt") as g:
                    json.dump(parsed, g)
                meta[wid] = {"status": "ok", "file_id": fid, "modified": workouts[wid].get("lastModifiedDate")}
                downloaded += 1
            except Exception as e:  # corrupt or unsupported file
                warnings.append(f"parse {wid}: {type(e).__name__}")
                meta[wid] = {"status": "error", "tries": prev.get("tries", 0) + 1}
            if downloaded % 50 == 0:
                (cache / "files_meta.json").write_text(json.dumps(meta))
        # drop cache entries for workouts deleted in TrainingPeaks
        for wid in list(meta):
            if wid not in workouts:
                meta.pop(wid)
                (cache / "files" / f"{wid}.json.gz").unlink(missing_ok=True)
        (cache / "files_meta.json").write_text(json.dumps(meta))

        # ---- health metrics
        metrics = []
        for a, b in _windows(start, today):
            r = await client.get(f"/metrics/v3/athletes/{aid}/consolidatedtimedmetrics/{a.isoformat()}/{b.isoformat()}")
            if _check(r, f"metrics {a}..{b}", warnings) and r.data:
                metrics.extend(r.data if isinstance(r.data, list) else [r.data])
        (cache / "metrics.json").write_text(json.dumps(metrics))

        # ---- events (races) from start to a year ahead
        events = []
        for a, b in _windows(start, today + dt.timedelta(days=365), 365):
            r = await client.get(f"/fitness/v6/athletes/{aid}/events/{a.isoformat()}/{b.isoformat()}")
            if _check(r, f"events {a}..{b}", warnings) and r.data:
                events.extend(r.data if isinstance(r.data, list) else [r.data])
        (cache / "events.json").write_text(json.dumps(events))

    return {"workouts": len(workouts), "downloaded": downloaded, "pending": max(0, len(todo) - MAX_DOWNLOADS),
            "metrics": len(metrics), "events": len(events), "warnings": warnings[:20]}


def run(cache: Path, start: dt.date, today: dt.date) -> dict:
    return asyncio.run(fetch(cache, start, today))

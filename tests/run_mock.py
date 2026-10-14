"""End-to-end test of the live path with a mocked TrainingPeaks API.

Serves a folder of raw device files (FIT/TCX, gzipped) as if they were TrainingPeaks workouts,
with synthetic HRV / resting HR / sleep metrics, one A-race event and some planned sessions.
Usage: DASH_PASSPHRASE=... python tests/run_mock.py RAW_DIR OUT_DIR TODAY
"""
import datetime as dt
import gzip
import hashlib
import math
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sync"))

from tp_mcp.client import http as tphttp  # noqa: E402
from fitparse_util import parse_activity_file  # noqa: E402

RAW, OUT, TODAY = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
random.seed(7)

files = sorted(RAW.glob("*.gz"))
WORK = {}
for i, p in enumerate(files):
    parsed = parse_activity_file(p.read_bytes())
    s = parsed["session"]
    st = s.get("start_time")
    if not st:
        continue
    wid = 1000 + i
    sport = s.get("sport")
    tt = {"running": 3, "cycling": 2, "swimming": 1, "training": 9, "hiking": 13}.get(sport, 100)
    tss = None if i % 5 == 0 else round((s.get("total_timer_time") or 0) / 3600 * random.uniform(40, 90), 1)
    title = "Parkrun #race" if i == 82 else ("3k loop #test" if i % 23 == 0 and sport == "running" else "")
    WORK[wid] = {"file": p, "w": {"workoutId": wid, "workoutDay": st[:10] + "T00:00:00", "title": title,
                                  "workoutTypeValueId": tt, "totalTime": (s.get("total_timer_time") or 0) / 3600,
                                  "distance": s.get("total_distance"), "tssActual": tss, "rpe": 6 if i % 7 == 0 else None,
                                  "lastModifiedDate": st, "description": "felt good" if i % 11 == 0 else None}}
# planned + day off
t0 = dt.date.fromisoformat(TODAY)
for k in range(1, 15):
    WORK[90000 + k] = {"file": None, "w": {"workoutId": 90000 + k, "workoutDay": (t0 + dt.timedelta(days=k)).isoformat() + "T00:00:00",
                                           "title": "planned", "workoutTypeValueId": 3, "totalTime": None, "tssPlanned": 45 + 5 * (k % 3)}}
WORK[99999] = {"file": None, "w": {"workoutId": 99999, "workoutDay": TODAY + "T00:00:00", "workoutTypeValueId": 7, "totalTime": 0}}

calls = {"get": 0, "raw": 0}


async def fake_athlete(self):
    return 123


async def fake_user(self):
    return {"firstName": "Mock", "lastName": "Athlete"}


async def fake_get(self, endpoint, params=None):
    calls["get"] += 1
    R = tphttp.APIResponse
    m = re.search(r"/workouts/(\d{4}-\d\d-\d\d)/(\d{4}-\d\d-\d\d)$", endpoint)
    if m:
        a, b = m.groups()
        return R(True, [v["w"] for v in WORK.values() if a <= v["w"]["workoutDay"][:10] <= b])
    m = re.search(r"/workouts/(\d+)/details$", endpoint)
    if m:
        wid = int(m.group(1))
        if wid % 17 == 0:
            return R(True, {"workoutDeviceFileInfos": []})
        return R(True, {"workoutDeviceFileInfos": [{"fileId": wid * 10, "fileName": "x.fit"}]})
    m = re.search(r"consolidatedtimedmetrics/(\d{4}-\d\d-\d\d)/(\d{4}-\d\d-\d\d)$", endpoint)
    if m:
        a, b = (dt.date.fromisoformat(x) for x in m.groups())
        out, d = [], a
        while d <= b:
            k = (d - dt.date(2022, 1, 1)).days
            if k % 9 != 4:
                hrv = 62 + 8 * math.sin(k / 9) + random.gauss(0, 5) - (10 if d.month == 8 else 0)
                out.append({"timeStamp": d.isoformat() + "T00:00:00", "details": [
                    {"type": 60, "value": round(hrv, 1)}, {"type": 5, "value": round(48 + random.gauss(0, 2) + (4 if d.month == 8 else 0))},
                    {"type": 6, "value": round(random.uniform(6.2, 8.4), 2)}]})
            d += dt.timedelta(days=1)
        return R(True, out)
    if "/events/" in endpoint:
        return R(True, [{"id": 1, "name": "County 10k", "eventDate": "2023-09-24T00:00:00", "priority": "A"},
                        {"id": 2, "name": "Boxing Day 5k", "eventDate": "2022-12-26T00:00:00", "priority": "B"}])
    return R(False, None, tphttp.ErrorCode.NOT_FOUND, "nf")


async def fake_raw(self, endpoint, params=None):
    calls["raw"] += 1
    wid = int(re.search(r"/workouts/(\d+)/rawfiledata", endpoint).group(1))
    return tphttp.RawResponse(True, WORK[wid]["file"].read_bytes(), "application/octet-stream", None)


tphttp.TPClient.ensure_athlete_id = fake_athlete
tphttp.TPClient._get_user_data = fake_user
tphttp.TPClient.get = fake_get
tphttp.TPClient.get_raw = fake_raw

import os  # noqa: E402
os.environ.setdefault("TP_START", "2022-08-25")
import run  # noqa: E402
sys.argv = ["run.py", "--out", OUT, "--cache", str(Path(OUT).parent / "mockcache"), "--today", TODAY]
run.main()
print("api calls", calls)

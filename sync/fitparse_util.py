"""Parse a FIT (optionally gzipped) or TCX file into a compact dict.

Output: {"src", "session", "laps", "rec", "rr"} where rec is a list of per-sample dicts.
"""
from __future__ import annotations

import datetime
import gzip
import io
import xml.etree.ElementTree as ET

import fitdecode

REC_FIELDS = ("timestamp", "heart_rate", "enhanced_speed", "enhanced_altitude", "distance",
              "cadence", "temperature", "enhanced_respiration_rate", "power")
LAP_FIELDS = ("start_time", "total_timer_time", "total_distance", "avg_heart_rate", "max_heart_rate",
              "enhanced_avg_speed", "total_ascent", "total_descent", "intensity", "lap_trigger", "avg_power")


def _clean(v):
    if isinstance(v, datetime.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=datetime.timezone.utc)
        return v.isoformat()
    if isinstance(v, (int, float, str)) or v is None:
        return v
    if isinstance(v, tuple):
        return [_clean(x) for x in v]
    return str(v)


def _maybe_gunzip(raw: bytes) -> bytes:
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def _parse_tcx(raw: bytes) -> dict:
    ns = {"t": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    root = ET.fromstring(raw)
    act = root.find(".//t:Activity", ns)
    out = {"src": "tcx", "session": {}, "laps": [], "rec": [], "rr": []}
    if act is None:
        return out
    s = out["session"]
    s["sport"] = (act.get("Sport") or "other").lower()
    s["sub_sport"] = "generic"
    idn = act.find("t:Id", ns)
    s["start_time"] = idn.text if idn is not None else None
    tot = 0.0; cal = 0; dist = 0.0
    for lap in act.findall("t:Lap", ns):
        tt = lap.find("t:TotalTimeSeconds", ns); dm = lap.find("t:DistanceMeters", ns); c = lap.find("t:Calories", ns)
        tot += float(tt.text) if tt is not None else 0
        dist += float(dm.text) if dm is not None else 0
        cal += int(c.text or 0) if c is not None else 0
    s.update(total_timer_time=tot, total_elapsed_time=tot, total_calories=cal, total_distance=dist)
    for tp in act.iter("{%s}Trackpoint" % ns["t"]):
        t = tp.find("t:Time", ns)
        hr = tp.find("t:HeartRateBpm/t:Value", ns)
        alt = tp.find("t:AltitudeMeters", ns)
        dd = tp.find("t:DistanceMeters", ns)
        r = {"timestamp": t.text if t is not None else None}
        if hr is not None: r["heart_rate"] = int(hr.text)
        if alt is not None: r["enhanced_altitude"] = float(alt.text)
        if dd is not None: r["distance"] = float(dd.text)
        out["rec"].append(r)
    return out


def parse_activity_file(raw: bytes) -> dict:
    raw = _maybe_gunzip(raw)
    if raw.lstrip()[:5] == b"<?xml" or b"<TrainingCenterDatabase" in raw[:500]:
        return _parse_tcx(raw)
    out = {"src": "fit", "session": {}, "laps": [], "rec": [], "rr": []}
    with fitdecode.FitReader(io.BytesIO(raw), check_crc=fitdecode.CrcCheck.DISABLED) as fr:
        for f in fr:
            if not isinstance(f, fitdecode.FitDataMessage):
                continue
            if f.name == "session" and not out["session"]:
                out["session"] = {fl.name: _clean(fl.value) for fl in f.fields
                                  if fl.value is not None and not fl.name.startswith("unknown")}
            elif f.name == "lap":
                out["laps"].append({fl.name: _clean(fl.value) for fl in f.fields if fl.name in LAP_FIELDS})
            elif f.name == "record":
                r = {}
                for fl in f.fields:
                    if fl.name in REC_FIELDS and fl.value is not None:
                        r[fl.name] = _clean(fl.value)
                out["rec"].append(r)
            elif f.name == "hrv":
                for fl in f.fields:
                    if fl.name == "time":
                        vals = fl.value if isinstance(fl.value, tuple) else (fl.value,)
                        out["rr"].extend(v for v in vals if v is not None)
    return out

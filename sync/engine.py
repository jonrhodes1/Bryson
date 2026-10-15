"""Analytics engine: TrainingPeaks cache (+ check-ins) -> dashboard data dict.

Load: TrainingPeaks TSS where TP has one; otherwise Banister TRIMP normalised so 1 h at LTHR = 100.
PMC: 42/7-day EWMA (CTL/ATL), TSB = yesterday's CTL - ATL.
Wellness: HRV (ln rMSSD 7-day mean vs 60-day normal range, Plews/Kiviniemi style), resting HR, sleep.
Performance markers for the personalised impulse-response fit: '#test' sessions if >= 12, else steady-run EF.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import nnls

TZ = "Europe/London"
STREAM_DAYS = 400          # keep second-by-second traces for this many days back (size control)

TP_SPORT = {1: "Swim", 2: "Bike", 3: "Run", 4: "Brick", 5: "Cardio", 6: "Race", 7: "DayOff", 8: "Bike",
            9: "Strength", 10: "Other", 11: "XC ski", 12: "Row", 13: "Walk", 29: "Strength", 100: "Other"}
FIT_LABEL = {"Trail Run": "Run", "Track Run": "Track", "Treadmill": "Treadmill", "Run": "Run", "Bike": "Bike",
             "Bike Indoor": "Indoor bike", "Strength": "Strength", "Hike": "Hike", "Pool Swim": "Swim",
             "Open Water": "Swim", "Cardio": "Cardio", "Surf": "Surf", "Walk": "Walk"}
FALLBACK_TSS_H = {"Swim": 50, "Strength": 40, "Hike": 35, "Walk": 25, "Surf": 40, "Cardio": 45, "Other": 45}
RUNS = ("Run", "Track", "Treadmill", "Race")
METRIC = {60: "hrv", 5: "rhr", 6: "sleep", 9: "weight"}
ZONES = [0, 0.85, 0.90, 0.95, 1.00, 9]
ZNAMES = ["Z1 Recovery", "Z2 Aerobic", "Z3 Tempo", "Z4 Threshold", "Z5 VO2+"]


# ------------------------------------------------------------------ helpers
def minetti(g):
    g = np.clip(g, -0.30, 0.30)
    return (155.4 * g**5 - 30.4 * g**4 - 43.3 * g**3 + 46.3 * g**2 + 19.5 * g + 3.6) / 3.6


def rnd(v, k=1):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, k) if math.isfinite(f) else None


def frame(fit):
    if not fit:
        return None
    r = [x for x in fit.get("rec", []) if x.get("timestamp")]
    if len(r) < 20:
        return None
    df = pd.DataFrame(r)
    df["t"] = pd.to_datetime(df.timestamp, format="ISO8601", utc=True, errors="coerce")
    df = df.dropna(subset=["t"]).drop_duplicates("t").set_index("t").sort_index()
    cols = [c for c in ["heart_rate", "enhanced_speed", "enhanced_altitude", "distance", "cadence", "power"] if c in df]
    if not cols:
        return None
    df = df[cols].apply(pd.to_numeric, errors="coerce")
    if len(df) < 20 or (df.index[-1] - df.index[0]).total_seconds() > 36 * 3600:
        return None
    full = df.resample("1s").mean().interpolate(limit=10, limit_area="inside")
    if "heart_rate" in full:
        full.loc[(full.heart_rate < 40) | (full.heart_rate > 215), "heart_rate"] = np.nan
    return full


def dfa_a1(rr):
    x = np.cumsum(rr - rr.mean()); ns = np.arange(4, 17); F = []
    for n in ns:
        m = len(x) // n
        if m < 2:
            return np.nan
        seg = x[:m * n].reshape(m, n); t = np.arange(n)
        c = np.polyfit(t, seg.T, 1)
        F.append(np.sqrt(np.mean((seg - (np.outer(c[0], t) + c[1][:, None])) ** 2)))
    return np.polyfit(np.log(ns), np.log(F), 1)[0]


def dfa_windows(rr_s):
    rr = np.array(rr_s, float); rr = rr[(rr > 0.25) & (rr < 2.0)]
    if len(rr) < 300:
        return None, None
    med = pd.Series(rr).rolling(11, center=True, min_periods=3).median().values
    art = np.abs(rr - med) / med > 0.20
    t = np.cumsum(rr); out = []
    for s0 in np.arange(0, t[-1] - 120, 30):
        m = (t >= s0) & (t < s0 + 120)
        if m.sum() < 60:
            continue
        clean = rr[m & ~art]
        if art[m].mean() > 0.05 or len(clean) < 60:
            continue
        out.append([round((s0 + 60) / 60, 2), round(60 / clean.mean(), 1), round(float(dfa_a1(clean)), 3)])
    return out, round(float(art.mean() * 100), 1)


def trimp_fn(hr_rest, hr_max):
    def f(hr):
        hrr = np.clip((hr - hr_rest) / (hr_max - hr_rest), 0, 1.05)
        return np.nansum(1 / 60 * hrr * 0.64 * np.exp(1.92 * hrr))
    return f


# ------------------------------------------------------------------ inputs
def items_from_cache(cache: Path, today: dt.date):
    workouts = json.loads((cache / "workouts.json").read_text())
    items, planned = [], {}
    for wid, w in workouts.items():
        day = str(w.get("workoutDay", ""))[:10]
        if not day:
            continue
        if w.get("totalTime") is None:          # planned, not completed
            if day >= today.isoformat() and (w.get("tssPlanned") or 0) > 0:
                planned[day] = planned.get(day, 0) + float(w["tssPlanned"])
            continue
        if day > today.isoformat() or w.get("workoutTypeValueId") == 7:
            continue
        fit = None
        p = cache / "files" / f"{wid}.json.gz"
        if p.exists():
            try:
                fit = json.load(gzip.open(p, "rt"))
            except Exception:
                fit = None
        items.append({"id": str(wid), "tp": w, "fit": fit})
    load = lambda n, d: json.loads((cache / n).read_text()) if (cache / n).exists() else d
    return items, planned, load("metrics.json", []), load("events.json", []), load("profile.json", {})


def items_from_export(path: Path):
    acts = json.load(gzip.open(path, "rt"))
    return [{"id": f"x{i}", "tp": None, "fit": a} for i, a in enumerate(acts)], {}, [], [], {}


def parse_metrics(metrics):
    rows = []
    for e in metrics or []:
        if not isinstance(e, dict):
            continue
        ts = str(e.get("timeStamp") or e.get("time") or "")[:10]
        details = e.get("details") if isinstance(e.get("details"), list) else [e]
        for d in details:
            if not isinstance(d, dict):
                continue
            k = METRIC.get(d.get("type"))
            v = d.get("value")
            day = str(d.get("time") or ts)[:10] or ts
            if k and v is not None and day:
                try:
                    rows.append((day, k, float(v)))
                except (TypeError, ValueError):
                    pass
    if not rows:
        return pd.DataFrame(columns=["hrv", "rhr", "sleep", "weight"])
    df = pd.DataFrame(rows, columns=["d", "k", "v"])
    return df.pivot_table(index="d", columns="k", values="v", aggfunc="mean")


def parse_events(events):
    out = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        d = str(e.get("eventDate") or e.get("date") or e.get("workoutDay") or "")[:10]
        if not d:
            continue
        pr = e.get("priority") or e.get("atpPriority") or e.get("eventPriority")
        pr = {1: "A", 2: "B", 3: "C"}.get(pr, pr)
        out.append({"date": d, "name": e.get("name") or e.get("title") or "Event", "priority": str(pr or "C").upper()[:1],
                    "type": e.get("eventType") or e.get("raceType"), "dist": e.get("distance"),
                    "ctl_target": e.get("ctlTarget")})
    seen, uniq = set(), []
    for e in sorted(out, key=lambda x: x["date"]):
        k = (e["date"], e["name"])
        if k not in seen:
            seen.add(k); uniq.append(e)
    return uniq


# ------------------------------------------------------------------ main build
def build(items, planned, metrics, events, profile, state, today: dt.date, sync_info=None):
    state = state or {}
    checkins = state.get("checkins", {}) or {}
    tags = state.get("races", {}) or {}
    for it in items:
        it["frame"] = frame(it["fit"])

    # ---- physiological anchors
    hr10, best20 = [], []
    for it in items:
        f = it["frame"]
        if f is None or "heart_rate" not in f or f.heart_rate.notna().sum() < 120:
            continue
        hr10.append(f.heart_rate.rolling(10, min_periods=8).mean().max())
        dur = len(f)
        sport = (it["fit"] or {}).get("session", {}).get("sport")
        if sport == "running" and dur >= 1500:
            best20.append(f.heart_rate.rolling(1200, min_periods=900).mean().max())
    hr10 = np.sort([x for x in hr10 if np.isfinite(x)])[::-1]
    best20 = np.sort([x for x in best20 if np.isfinite(x)])[::-1]
    settings = state.get("settings", {}) or {}
    HR_MAX = float(settings.get("hrmax") or (round(hr10[min(2, len(hr10) - 1)]) if len(hr10) else 190))
    LTHR = float(settings.get("lthr") or (round(best20[min(1, len(best20) - 1)]) if len(best20) else round(HR_MAX * 0.9)))
    met = parse_metrics(metrics)
    for d, c in checkins.items():                      # manual entries fill gaps in device metrics
        for src, k in (("hrv", "hrv"), ("rhr", "rhr"), ("sleep_h", "sleep")):
            if c.get(src) is not None:
                cur = met.at[d, k] if (d in met.index and k in met.columns) else np.nan
                if pd.isna(cur):
                    met.loc[d, k] = float(c[src])
    rhr_recent = met["rhr"].dropna().tail(60) if "rhr" in met else pd.Series(dtype=float)
    HR_REST = float(settings.get("hrrest") or (round(rhr_recent.median()) if len(rhr_recent) >= 7 else 50))
    HR_REST_SRC = "settings" if settings.get("hrrest") else ("measured" if len(rhr_recent) >= 7 else "assumed")
    trimp = trimp_fn(HR_REST, HR_MAX)
    T1H = trimp(np.full(3600, LTHR))

    stream_cut = (today - dt.timedelta(days=STREAM_DAYS)).isoformat()
    acts, ef_obs, tests, audit = [], [], [], []
    for it in items:
        tp, fit, f = it["tp"] or {}, it["fit"] or {}, it["frame"]
        s = fit.get("session", {}) if fit else {}
        # date/time
        if tp.get("workoutDay"):
            date = str(tp["workoutDay"])[:10]
        else:
            date = pd.Timestamp(s.get("start_time")).tz_convert(TZ).strftime("%Y-%m-%d")
        if s.get("start_time"):
            tm = pd.Timestamp(s["start_time"]).tz_convert(TZ).strftime("%H:%M")
        else:
            tm = str(tp.get("startTime") or "")[11:16]
        # sport label
        prof = s.get("sport_profile_name")
        label = FIT_LABEL.get(prof) if prof else None
        if not label:
            ss = (s.get("sub_sport") or "", s.get("sport") or "")
            label = {"treadmill": "Treadmill", "track": "Track"}.get(ss[0]) or {"running": "Run", "cycling": "Bike", "swimming": "Swim", "training": "Strength" if ss[0] == "strength_training" else "Cardio", "hiking": "Hike", "walking": "Walk"}.get(ss[1])
        if not label:
            label = TP_SPORT.get(tp.get("workoutTypeValueId"), "Other")
        title = (tp.get("title") or "").strip()
        is_run = label in RUNS or s.get("sport") == "running"
        dur_s = float(s.get("total_timer_time") or (tp.get("totalTime") or 0) * 3600)
        dist = float(s.get("total_distance") or tp.get("distance") or 0)
        a = {"id": it["id"], "date": date, "time": tm, "sport": label, "title": title or None,
             "dur": round(dur_s / 60, 1), "km": round(dist / 1000, 2),
             "asc": rnd(s.get("total_ascent") if s.get("total_ascent") is not None else tp.get("elevationGain"), 0),
             "hr": rnd(s.get("avg_heart_rate") or tp.get("heartRateAverage"), 0),
             "hrmax": rnd(s.get("max_heart_rate") or tp.get("heartRateMaximum"), 0),
             "te": s.get("total_training_effect"), "tea": s.get("total_anaerobic_training_effect"),
             "temp": s.get("avg_temperature"), "desc": (tp.get("description") or None)}
        rpe = tp.get("rpe") if tp.get("rpe") is not None else (round(s["workout_rpe"] / 10) if s.get("workout_rpe") is not None else None)
        feel = tp.get("feeling") if tp.get("feeling") is not None else (round(s["workout_feel"] / 25) + 1 if s.get("workout_feel") is not None else None)
        a["rpe"], a["feel"] = rpe, feel
        hr = f.heart_rate.values if (f is not None and "heart_rate" in f) else None
        has_hr = hr is not None and np.isfinite(hr).sum() > 60
        tp_tss = tp.get("tssActual")
        if tp_tss and tp_tss > 0:
            a["tss"], a["tss_src"] = round(float(tp_tss), 1), "tp"
        elif has_hr:
            a["tss"], a["tss_src"] = round(trimp(hr) / T1H * 100, 1), "hr"
        else:
            a["tss"], a["tss_src"] = round(dur_s / 3600 * FALLBACK_TSS_H.get(label, 45), 1), "est"
        if has_hr:
            z = np.isfinite(hr)
            a["zones"] = [round(float(((hr[z] >= LTHR * ZONES[k]) & (hr[z] < LTHR * ZONES[k + 1])).sum()) / 60, 1) for k in range(5)]
        a["srpe"] = round(rpe * dur_s / 60) if rpe else None
        a["test"] = "#test" in title.lower() or "#test" in (a["desc"] or "").lower()
        a["tagrace"] = "#race" in title.lower() or tp.get("workoutTypeValueId") == 6
        if f is not None:
            g = f.copy(); g["mins"] = (g.index - g.index[0]).total_seconds() / 60
            if is_run and "enhanced_speed" in g:
                spd = g.enhanced_speed.rolling(15, center=True, min_periods=5).mean()
                if label != "Treadmill" and "enhanced_altitude" in g and "distance" in g:
                    alt = g.enhanced_altitude.rolling(30, center=True, min_periods=5).mean()
                    dd = g.distance.diff(30); da = alt.diff(30)
                    grade = (da / dd).where(dd > 20, 0).fillna(0).clip(-0.3, 0.3)
                else:
                    grade = pd.Series(0.0, index=g.index)
                g["gap"] = spd * minetti(grade.values); g["spd"] = spd
                mv = (spd > 1.6) & (g.heart_rate.notna() if has_hr else True)
                if mv.sum() > 120:
                    a["pace"] = round(1000 / spd[mv].mean()); a["gap"] = round(1000 / g.gap[mv].mean())
                    if a["test"]:
                        tests.append((date, float(g.gap[mv].mean())))
                if "distance" in g and dist > 900:
                    dser = g.distance.ffill().values; tsec = np.arange(len(dser), dtype=float); be = {}
                    for D in (1000, 3000, 5000, 10000):
                        if dist < D * 0.98:
                            continue
                        j = np.searchsorted(dser, dser + D); ok = (j < len(dser)) & np.isfinite(dser)
                        if ok.any():
                            idx = np.where(ok)[0]; dts = tsec[j[idx]] - tsec[idx]; k = int(np.argmin(dts))
                            be[D] = int(dts[k])
                            if D in (3000, 5000) and has_hr:
                                seg = hr[idx[k]: idx[k] + int(dts[k])]
                                if np.isfinite(seg).any():
                                    be[f"hr{D}"] = round(float(np.nanmean(seg)))
                    a["best"] = be
                if has_hr and label in ("Run", "Track", "Treadmill"):
                    body = g.iloc[300:]
                    m2 = (body.spd > 1.6) & body.heart_rate.notna()
                    if m2.sum() >= 900:
                        hrm = body.heart_rate[m2].mean(); gp = body.gap[m2]
                        a["ef"] = round(gp.mean() * 60 / hrm, 3)
                        a["steady"] = bool(gp.std() / gp.mean() < 0.18 and hrm < 0.92 * LTHR)
                        if a["steady"]:
                            ef_obs.append((date, a["ef"]))
                            if m2.sum() >= 2400:
                                ix = np.where(m2.values)[0]; h = len(ix) // 2
                                e1 = body.gap.values[ix[:h]].mean() / body.heart_rate.values[ix[:h]].mean()
                                e2 = body.gap.values[ix[h:]].mean() / body.heart_rate.values[ix[h:]].mean()
                                a["decouple"] = round((e1 / e2 - 1) * 100, 1)
            if date >= stream_cut:
                n = len(g); step = max(1, int(math.ceil(n / 300))); ds = g.iloc[::step]
                st = {"t": [round(x, 2) for x in ds.mins]}
                if has_hr:
                    st["hr"] = [None if not np.isfinite(x) else int(round(x)) for x in ds.heart_rate]
                if "spd" in ds:
                    st["pace"] = [None if not (np.isfinite(x) and x > 1.2) else int(round(1000 / x)) for x in ds.spd]
                    st["gapace"] = [None if not (np.isfinite(x) and x > 1.2) else int(round(1000 / x)) for x in ds.gap]
                elif "enhanced_speed" in ds:
                    st["kph"] = [None if not np.isfinite(x) else round(x * 3.6, 1) for x in ds.enhanced_speed.rolling(5, min_periods=1).mean()]
                if "power" in ds and ds.power.notna().sum() > 10:
                    st["pw"] = [None if not np.isfinite(x) else int(round(x)) for x in ds.power.rolling(5, min_periods=1).mean()]
                if "enhanced_altitude" in ds and ds.enhanced_altitude.notna().sum() > 10:
                    st["alt"] = [None if not np.isfinite(x) else round(x, 1) for x in ds.enhanced_altitude]
                a["s"] = st
        if fit and fit.get("rr"):
            w, art = dfa_windows(fit["rr"])
            if w:
                audit.extend(w); a["dfa"] = {"art": art, "n": len(w), "a1": round(float(np.median(np.array(w)[:, 2])), 2)}
        acts.append(a)
    acts.sort(key=lambda x: (x["date"], x["time"] or ""))

    # ---- races: TP events, #race / TP race type, auto-detected maximal efforts, then manual tags win
    ev = parse_events(events)
    ev_days = {e["date"]: e for e in ev if e["priority"] in ("A", "B", "C")}
    by_day = {}
    for a in acts:
        by_day.setdefault(a["date"], []).append(a)
    for a in acts:
        b = a.get("best", {}); auto = False
        for D in (5000, 3000):
            if D in b and b.get(f"hr{D}") and b[f"hr{D}"] >= 0.97 * LTHR:
                auto = True; break
        a["race_auto"] = auto
        a["isRace"] = bool(a["tagrace"] or auto)
    for d, e in ev_days.items():
        cands = sorted(by_day.get(d, []), key=lambda x: -x["tss"])
        if cands:
            cands[0]["isRace"] = True; cands[0]["event"] = e["name"]; cands[0]["priority"] = e["priority"]
    for a in acts:
        a["raceBase"] = a["isRace"]
        if a["id"] in tags:
            t = tags[a["id"]]
            a["isRace"] = bool(t.get("v") if isinstance(t, dict) else t)

    # ---- daily series
    if acts:
        d0 = pd.Timestamp(min(a["date"] for a in acts))
    else:
        d0 = pd.Timestamp(today - dt.timedelta(days=90))
    days = pd.date_range(d0, pd.Timestamp(today), freq="D")
    df = pd.DataFrame(acts) if acts else pd.DataFrame(columns=["date", "tss", "dur", "km", "sport", "zones", "id", "asc", "srpe"])
    df["date"] = pd.to_datetime(df.date)
    day = pd.DataFrame(index=days)
    day["tss"] = df.groupby("date").tss.sum().reindex(days).fillna(0)
    day["mins"] = df.groupby("date").dur.sum().reindex(days).fillna(0)
    day["km"] = df[df.sport.isin(RUNS)].groupby("date").km.sum().reindex(days).fillna(0)
    ctl = atl = float(day.tss.iloc[:28].mean()); C, A_, T = [], [], []
    for v in day.tss.values:
        T.append(ctl - atl); ctl += (v - ctl) / 42; atl += (v - atl) / 7; C.append(ctl); A_.append(atl)
    day["ctl"], day["atl"], day["tsb"] = C, A_, T
    a7 = day.tss.ewm(alpha=2 / 8, adjust=False).mean(); a28 = day.tss.ewm(alpha=2 / 29, adjust=False).mean()
    day["acwr"] = (a7 / a28).where(a28 > 5)
    r7 = day.tss.rolling(7); day["mono"] = (r7.mean() / r7.std()).replace([np.inf, -np.inf], np.nan)

    # ---- wellness channels
    met.index = pd.to_datetime(met.index)
    met = met[~met.index.duplicated()].reindex(days)
    for k in ("hrv", "rhr", "sleep", "weight"):
        if k not in met:
            met[k] = np.nan
    met = met.apply(pd.to_numeric, errors="coerce").astype(float)   # empty metrics arrive as object dtype
    ln = np.log(met.hrv.where(met.hrv > 0))
    ln7 = ln.rolling(7, min_periods=3).mean()
    base = ln.shift(1).rolling(60, min_periods=14).mean(); sd = ln.shift(1).rolling(60, min_periods=14).std()
    rhr30 = met.rhr.shift(1).rolling(30, min_periods=7).mean()
    well = {"hrv": [rnd(v) for v in met.hrv], "rhr": [rnd(v, 0) for v in met.rhr], "sleep": [rnd(v, 2) for v in met.sleep],
            "hrv7": [rnd(math.exp(v)) if pd.notna(v) else None for v in ln7],
            "hrv_lo": [rnd(math.exp(b - 0.5 * s)) if pd.notna(b) and pd.notna(s) else None for b, s in zip(base, sd)],
            "hrv_hi": [rnd(math.exp(b + 0.5 * s)) if pd.notna(b) and pd.notna(s) else None for b, s in zip(base, sd)],
            "hrv_z": [rnd((v - b) / s, 2) if pd.notna(v) and pd.notna(b) and pd.notna(s) and s > 0 else None for v, b, s in zip(ln7, base, sd)],
            "rhr30": [rnd(v, 1) for v in rhr30], "weight": [rnd(v) for v in met.weight]}

    # ---- weekly
    wk = df.copy(); wk["wk"] = wk.date - pd.to_timedelta(wk.date.dt.weekday, unit="D")
    zl = wk.zones.dropna() if "zones" in wk else pd.Series(dtype=object)
    zc = pd.DataFrame(zl.tolist(), index=zl.index, columns=ZNAMES) if len(zl) else pd.DataFrame(columns=ZNAMES)
    wk = wk.join(zc)
    weekly = wk.groupby("wk").agg(tss=("tss", "sum"), hrs=("dur", lambda x: x.sum() / 60),
                                  km=("km", lambda x: x[wk.loc[x.index, "sport"].isin(RUNS)].sum()),
                                  asc=("asc", lambda x: pd.to_numeric(x, errors="coerce").sum()),
                                  **{z: (z, "sum") for z in ZNAMES}) if len(wk) else pd.DataFrame()
    if len(weekly):
        weekly = weekly.reindex(pd.date_range(weekly.index.min(), weekly.index.max(), freq="7D")).fillna(0)

    # ---- personalised impulse-response fit
    model = fit_model(day, tests, ef_obs)

    AW = np.array(audit) if audit else np.zeros((0, 3))
    bands = []
    for lo in range(100, 200, 10):
        m = (AW[:, 1] >= lo) & (AW[:, 1] < lo + 10) if len(AW) else np.array([], bool)
        if m.sum():
            bands.append([lo, int(m.sum()), round(float(np.median(AW[m, 2])), 2)])
    hi_band = [b for b in bands if b[0] >= 160]
    valid = bool(np.median([b[2] for b in hi_band]) < 0.75) if hi_band else None

    up = [e for e in ev if e["date"] >= today.isoformat()]
    planned_out = {k: round(v, 1) for k, v in sorted(planned.items())}
    out = {
        "athlete": {"name": profile.get("name"), "hrmax": HR_MAX, "lthr": LTHR, "hrrest": HR_REST, "hrrest_src": HR_REST_SRC,
                    "zones": [round(LTHR * z) for z in ZONES[1:5]], "znames": ZNAMES},
        "range": [str(days[0].date()), str(days[-1].date())], "today": today.isoformat(), "live": sync_info is not None,
        "acts": acts,
        "days": {"d": [str(x.date()) for x in days], "tss": [rnd(v) for v in day.tss], "ctl": [rnd(v) for v in day.ctl],
                 "atl": [rnd(v) for v in day.atl], "tsb": [rnd(v) for v in day.tsb], "acwr": [rnd(v, 2) for v in day.acwr],
                 "mono": [rnd(v, 2) for v in day.mono], "mins": [rnd(v, 0) for v in day.mins], "km": [rnd(v, 1) for v in day.km]},
        "well": well,
        "weekly": {"wk": [str(x.date()) for x in weekly.index], "tss": [rnd(v, 0) for v in weekly.get("tss", [])],
                   "hrs": [rnd(v, 1) for v in weekly.get("hrs", [])], "km": [rnd(v, 1) for v in weekly.get("km", [])],
                   "asc": [rnd(v, 0) for v in weekly.get("asc", [])],
                   "zones": [[rnd(v, 0) for v in weekly[z]] if z in weekly else [] for z in ZNAMES]},
        "ef": {"d": [d for d, _ in ef_obs], "v": [round(v, 4) for _, v in ef_obs]},
        "tests": [{"date": d, "v": round(v, 3)} for d, v in tests],
        "events": ev, "upcoming": up[:12], "planned": planned_out,
        "audit": {"sessions": sum(1 for a in acts if a.get("dfa")), "windows": int(len(AW)), "bands": bands, "valid": valid},
        "model": model, "sync": sync_info or {"mode": "export"},
    }
    for a in out["acts"]:
        for k in [k for k, v in a.items() if v is None or v is False]:
            if k not in ("isRace", "raceBase"):
                a.pop(k)
    return out


def fit_model(day, tests, ef_obs):
    use_tests = len(tests) >= 20
    obs = tests if use_tests else ef_obs
    src = "tests" if use_tests else "ef"
    base = {"source": src, "n_tests": len(tests), "n": len(obs), "status": "insufficient"}
    if len(obs) < 20:
        return base
    s = pd.Series([v for _, v in obs], index=pd.to_datetime([d for d, _ in obs])).groupby(level=0).mean()
    med = s.median(); mad = (s - med).abs().median() * 1.4826
    if mad > 0:
        s = s[(s - med).abs() < 3.5 * mad]
    w = day.tss.values; tix = {d: k for k, d in enumerate(day.index)}
    keep = [d for d in s.index if d in tix]
    k_idx = np.array([tix[d] for d in keep]); y = s.loc[keep].values
    if len(y) < 20:
        return base

    def conv(tau):
        out = np.zeros(len(w)); acc = 0.0; dec = math.exp(-1 / tau)
        for k in range(len(w)):
            out[k] = acc; acc = acc * dec + w[k]
        return out
    G = {t: conv(t) for t in range(2, 61)}

    def fit(t1, t2, ki, yy):
        g, h = G[t1][ki], G[t2][ki]
        X = np.column_stack([np.ones_like(g), -np.ones_like(g), g, -h])
        c, _ = nnls(X, yy)
        p0, k1, k2 = c[0] - c[1], c[2], c[3]
        return p0, k1, k2, p0 + k1 * g - k2 * h

    def search(ki, yy):
        best = None
        for t1 in range(20, 61, 2):
            for t2 in range(2, 19):
                if t2 >= t1:
                    continue
                p0, k1, k2, pr = fit(t1, t2, ki, yy); sse = ((yy - pr) ** 2).sum()
                if best is None or sse < best[0]:
                    best = (sse, t1, t2, p0, k1, k2)
        return best
    sse, t1, t2, p0, k1, k2 = search(k_idx, y)
    sst = ((y - y.mean()) ** 2).sum(); r2 = 1 - sse / sst if sst > 0 else 0
    lt = np.polyfit(k_idx, y, 1); r2t = 1 - ((y - np.polyval(lt, k_idx)) ** 2).sum() / sst if sst > 0 else 0
    cut = int(len(y) * 0.75); bt = search(k_idx[:cut], y[:cut])
    pr = bt[3] + bt[4] * G[bt[1]][k_idx[cut:]] - bt[5] * G[bt[2]][k_idx[cut:]]
    mae, mae0 = float(np.abs(y[cut:] - pr).mean()), float(np.abs(y[cut:] - y[:cut].mean()).mean())
    tn = t1 * t2 / (t1 - t2) * math.log(k2 / k1) if k1 > 0 and k2 > k1 else 0.0
    tp = t1 * t2 / (t1 - t2) * math.log((k2 * t1) / (k1 * t2)) if k1 > 0 and k2 * t1 > k1 * t2 else 0.0
    boundary = t1 in (20, 60) or t2 in (2, 18)
    ok = r2 >= 0.25 and r2 > r2t + 0.05 and mae < 0.9 * mae0 and not boundary and k1 > 0 and k2 > 0
    return {**base, "status": "personal" if ok else "not_identifiable", "tau1": t1, "tau2": t2, "k1": k1, "k2": k2, "p0": p0,
            "r2": round(r2, 3), "r2_trend": round(float(r2t), 3), "mae_oos": round(mae, 4), "mae_naive": round(mae0, 4),
            "t_neg": round(tn, 1), "t_peak": round(tp, 1), "boundary": boundary}

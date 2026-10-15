"""Nightly build: fetch TrainingPeaks -> analyse -> encrypt -> write the static site.

Usage:
  python sync/run.py --out _site                    # live (needs TP_AUTH_COOKIE, DASH_PASSPHRASE)
  python sync/run.py --out _site --export FILE      # offline test from a parsed export
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import crypto  # noqa: E402
import engine  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_site")
    ap.add_argument("--cache", default=str(HERE / "cache"))
    ap.add_argument("--export", default=None, help="offline mode: parsed export (activities.json.gz)")
    ap.add_argument("--today", default=None)
    args = ap.parse_args()

    pw = os.environ.get("DASH_PASSPHRASE")
    if not pw or len(pw) < 16:
        sys.exit("DASH_PASSPHRASE secret missing or shorter than 16 characters. Use 4+ random words.")
    cfg = json.loads((ROOT / "site" / "crypto.json").read_text())
    key = crypto.derive_key(pw, cfg["salt"], cfg["iter"])

    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(ZoneInfo("Europe/London")).date()
    state = {}
    sp = ROOT / "state.enc"
    if sp.exists() and sp.stat().st_size > 0:
        try:
            state = crypto.decrypt_json(sp.read_text(), key)
        except Exception:
            print("WARNING: state.enc could not be decrypted with DASH_PASSPHRASE (passphrase changed?). Ignoring it.")

    cache = Path(args.cache)
    if args.export:
        items, planned, metrics, events, profile = engine.items_from_export(Path(args.export))
        info = None
        if args.today is None:
            today = max(dt.date.fromisoformat(str(i["fit"]["session"].get("start_time", "1970-01-01"))[:10]) for i in items if i["fit"].get("session"))
    else:
        import fetch
        start = dt.date.fromisoformat(os.environ["TP_START"]) if os.environ.get("TP_START") else today - dt.timedelta(days=730)
        try:
            summary = fetch.run(cache, start, today)
        except fetch.SyncAuthError as e:
            print(f"::error title=TrainingPeaks sync failed::{e}")   # shows on the run page, not just in the log
            sys.exit(f"AUTH: {e}")
        print("fetch:", json.dumps(summary))
        items, planned, metrics, events, profile = engine.items_from_cache(cache, today)
        info = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "mode": "live", **summary}

    data = engine.build(items, planned, metrics, events, profile, state, today, info)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    (out / "data.enc").write_text(crypto.encrypt_json(data, key))
    for f in ("index.html", "crypto.json"):
        shutil.copy(ROOT / "site" / f, out / f)
    if sp.exists():
        shutil.copy(sp, out / "state.enc")
    (out / ".nojekyll").write_text("")
    print(f"built {len(data['acts'])} activities, {len(data['days']['d'])} days -> {out}/data.enc "
          f"({(out / 'data.enc').stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()

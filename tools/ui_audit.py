"""Screen-by-screen button audit: open every screen of the app on a demo database and press every
button, menu item, filter, tab, double-click and right-click on it (and inside the windows they
open, two levels deep). Each screen runs in its own process on its own fresh copy of the demo data.

    python -m tools.ui_audit [out_folder] [screen ...]

Prints one line per screen and exits with 1 if any screen had an error (a Python exception, an
error in the log, a red error box, a crash or a hang). The details per screen are in
<out_folder>/<screen>.json. Message boxes are answered Yes / OK; file dialogs save into a temp
folder; "Restore" of the whole database and "Log Out" are never pressed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCREENS = ["_main", "dashboard", "products", "locations", "goods_in", "goods_out", "projects", "sites",
           "purchase_orders", "delivery_notes", "stocktake", "returns", "finished", "reports", "users",
           "audit", "settings", "search", "low_stock", "reservations", "discrepancies", "movements"]
ROOT = Path(__file__).resolve().parent.parent


def run_one(key: str, out: Path) -> dict:
    target = out / f"{key}.json"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(ROOT))
    try:
        proc = subprocess.run([sys.executable, "-X", "faulthandler", "-m", "tools.ui_audit_page", key,
                               str(target)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
        code = proc.returncode
        tail = proc.stderr[-2000:]
    except subprocess.TimeoutExpired:
        code, tail = "timeout", ""
    try:
        r = json.loads(target.read_text())
    except Exception:  # noqa: BLE001
        r = {"page": key, "errors": [], "clicked": [], "dialogs": [], "partial": True}
    if code != 0 or r.get("partial"):
        r["errors"].append({"where": key, "kind": "CRASH" if code != "timeout" else "HANG",
                            "detail": f"exit {code}\n{tail}"})
    return r


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else Path(tempfile.mkdtemp(prefix="ui_audit_"))
    out.mkdir(parents=True, exist_ok=True)
    screens = argv[1:] or SCREENS
    with ThreadPoolExecutor(max_workers=max(1, min(3, (os.cpu_count() or 2) - 1))) as pool:
        results = list(pool.map(lambda k: run_one(k, out), screens))
    bad = 0
    for r in results:
        errs = r["errors"]
        bad += bool(errs)
        print(f"{'FAIL' if errs else 'ok  '}  {r['page']:<16} {len(set(r['clicked'])):>4} actions  "
              f"{len({d['dialog'] for d in r['dialogs']}):>3} window types  {len(errs)} errors")
        for e in errs[:5]:
            print(f"        {e['kind']}: {e['where'][:160]}\n          "
                  f"{e['detail'].strip().splitlines()[-1][:200] if e['detail'].strip() else ''}")
    print(f"\nDetails: {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

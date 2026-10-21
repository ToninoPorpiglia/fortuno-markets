"""
cloud_run.py  -  runs Fortuno AI on GitHub's computers (so your Mac can be closed).
GitHub starts this at set times. It checks the clock (New York time) and decides what to do:

  morning  (starts ~7:47 AM ET)  scanner, then the Premarket Brief, sent at exactly 9:00 AM
  alerts1  (starts ~8:32 AM ET)  breakout alerts, until 2:55 PM
  alerts2  (starts ~2:07 PM ET)  breakout alerts, until the 4:00 PM close
  evening  (starts ~3:33 PM ET)  After-Close Brief, built after 4:10 PM, sent at exactly 4:30 PM

GitHub's schedule only knows UTC, so we schedule each job twice (summer + winter time) and
this file ignores the run that lands at the wrong hour. It also skips weekends and market holidays.
"""
import os, sys, time, subprocess, datetime as dt, glob
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")

# the cron text in fortuno.yml  ->  (task, hour ET, minute ET). Start times are EARLY on purpose:
# GitHub often starts jobs 20-35 minutes late, and the briefs wait until the exact send time.
SCHEDULES = {
    "47 11 * * 1-5": ("morning", 7, 47), "47 12 * * 1-5": ("morning", 7, 47),
    "32 12 * * 1-5": ("alerts1", 8, 32), "32 13 * * 1-5": ("alerts1", 8, 32),
    "7 18 * * 1-5": ("alerts2", 14, 7), "7 19 * * 1-5": ("alerts2", 14, 7),
    "33 19 * * 1-5": ("evening", 15, 33), "33 20 * * 1-5": ("evening", 15, 33),
}
EARLY_MIN, LATE_MIN = 10, 50     # a run is accepted from 10 min before to 50 min after its planned time

# NYSE closed days (double-check these each January)
HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
}
SCAN_TIME_LIMIT = 35 * 60        # seconds. If the scanner is slower than this, we move on without it


def run(cmd, timeout=None):
    print("\n$ " + " ".join(cmd), flush=True)
    try:
        return subprocess.run(cmd, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print(f"  (stopped after {timeout // 60} minutes)")
        return 124


def clean_old_files(now):
    for f in glob.glob("alerts_sent_*.json"):
        try:
            d = dt.date.fromisoformat(f[len("alerts_sent_"):-len(".json")])
            if (now.date() - d).days > 3:
                os.remove(f)
        except Exception:
            pass


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True)


def commit_push(paths, msg):
    """Save files to GitHub right now (so other jobs can see them)."""
    git("config", "user.name", "fortuno-bot")
    git("config", "user.email", "fortuno-bot@users.noreply.github.com")
    for f in paths:
        if os.path.exists(f):
            git("add", "--", f)
    git("commit", "-m", msg)
    for _ in range(3):
        if git("pull", "--rebase", "--autostash", "origin", "main").returncode == 0 and git("push").returncode == 0:
            return
        time.sleep(5)


def wait_for_todays_scan(now):
    """The alert job starts before the scan is done. Wait (until 9:25 AM ET) for today's scan to show up on GitHub."""
    today = f"{now:%Y-%m-%d}"
    deadline = now.replace(hour=9, minute=25, second=0, microsecond=0)
    while True:
        try:
            have = open("scan_date.txt").read().strip()
        except Exception:
            have = ""
        if have == today or dt.datetime.now(TZ) >= deadline:
            return
        print("  waiting for this morning's scan...", flush=True)
        time.sleep(45)
        git("pull", "--rebase", "--autostash", "origin", "main")


def do_task(task, now):
    py = sys.executable
    if task == "morning":
        before = os.path.getmtime("scan_results.csv") if os.path.exists("scan_results.csv") else 0
        rc = run([py, "breakout_scanner.py"], timeout=SCAN_TIME_LIMIT)
        after = os.path.getmtime("scan_results.csv") if os.path.exists("scan_results.csv") else 0
        if rc == 0 and after > before:
            open("scan_date.txt", "w").write(f"{now:%Y-%m-%d}")
            print("Scan finished and saved.")
            commit_push(["scan_results.csv", "scan_all_passers.csv", "scan_date.txt"], f"Scan {now:%Y-%m-%d}")
        else:
            print("WARNING: the scan did not finish. The brief will use the last saved scan.")
        run([py, "market_brief.py", "--morning", "--not-before", "08:55", "--publish-at", "09:00"])
    elif task == "evening":
        run([py, "market_brief.py", "--evening", "--not-before", "16:10", "--publish-at", "16:30"])
    elif task == "alerts1":
        wait_for_todays_scan(now)
        run([py, "breakout_alerts.py", "--stop-at", "14:55"])
    elif task == "alerts2":
        wait_for_todays_scan(now)
        run([py, "breakout_alerts.py"])
    elif task == "alerts":
        run([py, "breakout_alerts.py"])
    elif task == "alert-selftest":
        run([py, "breakout_alerts.py", "--selftest"])
    elif task == "demo-alert":
        run([py, "breakout_alerts.py", "--demo"])
    else:
        print(f"Unknown task: {task}")


def main():
    now = dt.datetime.now(TZ)
    clean_old_files(now)
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event == "workflow_dispatch":          # you pressed "Run workflow" yourself: do it, no clock checks
        task = os.environ.get("TASK", "demo-alert")
        print(f"Manual run: {task}")
        do_task(task, now)
        return
    sched = os.environ.get("SCHEDULE", "")
    if sched not in SCHEDULES:
        print(f"Not a known schedule ({sched!r}). Nothing to do.")
        return
    task, h, m = SCHEDULES[sched]
    planned = now.replace(hour=h, minute=m, second=0, microsecond=0)
    mins_late = (now - planned).total_seconds() / 60
    print(f"It is {now:%a %Y-%m-%d %I:%M %p} ET. Task {task} was planned for {h}:{m:02d} ({mins_late:+.0f} min).")
    if now.weekday() >= 5 or f"{now:%Y-%m-%d}" in HOLIDAYS:
        print("Market is closed today. Skipping.")
        return
    if not (-EARLY_MIN <= mins_late <= LATE_MIN):
        print("This run landed at the wrong hour (the other daylight-saving copy, or GitHub was very late). Skipping.")
        return
    do_task(task, now)


if __name__ == "__main__":
    main()

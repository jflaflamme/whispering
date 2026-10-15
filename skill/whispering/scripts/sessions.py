#!/usr/bin/env python3
"""List whispering transcripts in ~/transcripts as calls.

One row per transcript: start, end, line counts per side, real mic lines
(filler like "Thank you." excluded), whether it is still being written, and
whether a summary already names it. Files that follow each other within
--gap minutes are grouped as one call (a restart mid-call makes a new file).

Usage: sessions.py [--date YYYY-MM-DD] [--gap MINUTES] [--dir PATH]
Read-only: it never changes or moves a file.
"""
import argparse
import datetime as dt
import re
import subprocess
from pathlib import Path

LINE = re.compile(r"^(\d\d:\d\d:\d\d) \[(mic|spk)(?: [A-Z?+]+)?\] (.*)$")
FILLER = re.compile(r"^(thank you\.?|thanks\.?|\.+|…+|so|um\.*|uh\.*|yeah\.?|okay\.?)( (thank you\.?|\.+))*$", re.I)
NAME = re.compile(r"^(\d{4}-\d\d-\d\d)_(\d{4})(?:_(.+))?\.txt$")


def in_use(path):
    """True while a process (a running whispering) has the file open."""
    r = subprocess.run(["fuser", str(path)], capture_output=True, text=True)
    return r.returncode == 0


def scan(path):
    times, mic, spk, real_mic = [], 0, 0, 0
    for raw in path.read_text(errors="replace").splitlines():
        m = LINE.match(raw)
        if not m:
            continue
        t, side, text = m.groups()
        times.append(t)
        if side == "mic":
            mic += 1
            if not FILLER.match(text.replace(" [cut]", "").strip()):
                real_mic += 1
        else:
            spk += 1
    return (times[0] if times else "-", times[-1] if times else "-", mic, spk, real_mic)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", help="only this day (YYYY-MM-DD)")
    p.add_argument("--gap", type=int, default=20, help="minutes between files of the same call (default 20)")
    p.add_argument("--dir", default=str(Path.home() / "transcripts"))
    a = p.parse_args()
    d = Path(a.dir)

    summaries = {s: s.read_text(errors="replace") for s in d.glob("*.md")}
    rows = []
    for f in sorted(d.glob("*.txt")):
        m = NAME.match(f.name)
        if not m or (a.date and m.group(1) != a.date):
            continue
        start, end, mic, spk, real_mic = scan(f)
        named_in = [s.name for s, text in summaries.items() if f.name in text]
        rows.append((m.group(1), f, start, end, mic, spk, real_mic, in_use(f), named_in))

    call, prev = 0, None
    for day, f, start, end, mic, spk, real_mic, live, named_in in rows:
        if start != "-" and prev and prev[0] == day and prev[1] != "-":
            gap = (dt.datetime.strptime(start, "%H:%M:%S") - dt.datetime.strptime(prev[1], "%H:%M:%S")).total_seconds() / 60
            if gap > a.gap:
                call += 1
        elif prev is None or prev[0] != day:
            call += 1
        prev = (day, end)
        flags = []
        if live:
            flags.append("RECORDING NOW")
        if named_in:
            flags.append("summary: " + ", ".join(named_in))
        print(f"call {call:>2} | {f.name:45} | {start}-{end} | mic {mic:>3} ({real_mic} real) | spk {spk:>3} | {' | '.join(flags)}")


if __name__ == "__main__":
    main()

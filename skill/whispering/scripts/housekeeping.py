#!/usr/bin/env python3
"""Disk use of whispering transcripts and audio in ~/transcripts, and optional cleanup.

Without options it only reports: per month, how many transcripts, summaries and audio
files, and how much space they take; WAV files that could be compressed; audio older
than --days. Nothing changes unless --yes is given with an action:

  housekeeping.py                          report only
  housekeeping.py --compress --yes         WAV -> Opus (needs ffmpeg), WAV deleted after
  housekeeping.py --delete-audio --days 90 --yes
                                           delete audio (.opus/.wav) older than 90 days;
                                           transcripts and summaries are never deleted

Files still being written by a running whispering are always skipped.
"""
import argparse
import datetime as dt
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

DAY = re.compile(r"^(\d{4}-\d\d)-\d\d_")
AUDIO = re.compile(r"\.(mic|spk)\.(opus|wav)$")
PLURAL = {"transcript": "transcripts", "summary": "summaries", "audio": "audio files", "other": "others"}


def in_use(path):
    return subprocess.run(["fuser", str(path)], capture_output=True).returncode == 0


def mb(n):
    return f"{n / 1e6:,.1f} MB"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dir", default=str(Path.home() / "transcripts"))
    p.add_argument("--days", type=int, default=90, help="age for --delete-audio (default 90)")
    p.add_argument("--compress", action="store_true", help="compress WAV audio to Opus")
    p.add_argument("--delete-audio", action="store_true", help="delete audio older than --days")
    p.add_argument("--yes", action="store_true", help="really do it (otherwise: show what would happen)")
    a = p.parse_args()
    d = Path(a.dir)

    months = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # month -> kind -> [count, bytes]
    wavs, old = [], []
    cutoff = dt.datetime.now() - dt.timedelta(days=a.days)
    for f in sorted(d.glob("*")):
        if not f.is_file():
            continue
        m = DAY.match(f.name)
        month = m.group(1) if m else "other"
        kind = "audio" if AUDIO.search(f.name) else "summary" if f.suffix == ".md" else \
            "transcript" if f.suffix == ".txt" else "other"
        size = f.stat().st_size
        months[month][kind][0] += 1
        months[month][kind][1] += size
        if kind == "audio":
            if f.suffix == ".wav":
                wavs.append(f)
            if dt.datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                old.append(f)

    total = 0
    for month in sorted(months):
        parts = []
        for kind in ("transcript", "summary", "audio", "other"):
            n, size = months[month][kind]
            if n:
                parts.append(f"{n} {kind if n == 1 else PLURAL[kind]} {mb(size)}")
                total += size
        print(f"{month:8} | " + " | ".join(parts))
    print(f"total    | {mb(total)} in {d}")

    if wavs:
        print(f"\nWAV audio not compressed: {len(wavs)} files, {mb(sum(f.stat().st_size for f in wavs))}"
              " (Opus is about a tenth of that)")
    if old:
        print(f"Audio older than {a.days} days: {len(old)} files, {mb(sum(f.stat().st_size for f in old))}")

    if a.compress:
        if not shutil.which("ffmpeg"):
            raise SystemExit("ffmpeg not found: install it to compress")
        for w in wavs:
            opus = w.with_suffix(".opus")
            if in_use(w) or opus.exists():
                print(f"skip {w.name} ({'recording now' if in_use(w) else opus.name + ' exists'})")
                continue
            if not a.yes:
                print(f"would compress {w.name}")
                continue
            r = subprocess.run(["ffmpeg", "-loglevel", "error", "-nostdin", "-i", str(w), "-c:a", "libopus",
                                "-b:a", "24k", "-application", "voip", str(opus)])
            if r.returncode == 0 and opus.exists() and opus.stat().st_size:
                print(f"compressed {w.name}: {mb(w.stat().st_size)} -> {mb(opus.stat().st_size)}")
                w.unlink()
            else:
                opus.unlink(missing_ok=True)
                print(f"failed: {w.name} (kept)")

    if a.delete_audio:
        for f in old:
            if not f.exists():
                continue
            if in_use(f):
                print(f"skip {f.name} (recording now)")
            elif not a.yes:
                print(f"would delete {f.name}")
            else:
                f.unlink()
                print(f"deleted {f.name}")

    if (a.compress or a.delete_audio) and not a.yes:
        print("\nNothing changed. Add --yes to do it.")


if __name__ == "__main__":
    main()

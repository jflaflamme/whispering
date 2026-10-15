#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=12"]
# ///
"""Live transcription of mic + speaker monitor through a local Lemonade server.

Each PulseAudio/PipeWire source is captured with `parec` (16 kHz mono s16le)
and streamed to Lemonade's OpenAI-compatible realtime endpoint over its own
WebSocket session. Final lines scroll; partial text sits in a live footer.
"""

import argparse
import array
import asyncio
import base64
import collections
import datetime as dt
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websockets

RATE = 16000
CHUNK_BYTES = RATE * 2 // 10  # 100 ms of s16le mono

COLORS = {"mic": "\033[1;36m", "spk": "\033[1;33m"}
RESET, DIM = "\033[0m", "\033[2m"

KM_MODEL = "1morecupofhottea/whisper-turbo-khmer-v9"
KM_PORT = 13399
KM_MAX_SECONDS = 25  # cut long unbroken speech so the model stays under its 30 s window
KM_PENDING = []  # (stream, pcm, cut) left over at Ctrl+C, transcribed before exit


OUR_APPS = {"pacat", "parec", "ffmpeg", "python3", "python"}  # our own recorders, not the call


def pactl_json(*args):
    try:
        out = subprocess.run(["pactl", "-f", "json", *args], capture_output=True, text=True).stdout
        return json.loads(out or "[]")
    except (OSError, ValueError):
        return []


def default_sources():
    """Pick the mic and speaker the call is using: the newest recording stream from
    another app (the call app), and the output that same app plays to. Falls back to
    the system defaults. Returns (mic, spk, why)."""
    def get(cmd):
        return subprocess.run(["pactl", cmd], capture_output=True, text=True).stdout.strip()

    mic, spk, why = get("get-default-source"), get("get-default-sink") + ".monitor", "system default"
    sources = {s["index"]: s["name"] for s in pactl_json("list", "sources")}
    monitors = {s["index"]: s.get("monitor_source") for s in pactl_json("list", "sinks")}

    def streams(kind, key):
        found = []
        for o in sorted(pactl_json("list", kind), key=lambda o: o["index"]):
            props = o.get("properties", {})
            binary = props.get("application.process.binary", "")
            if binary in OUR_APPS or o.get("corked"):
                continue
            found.append((binary, props.get("application.name", binary), o[key]))
        return found

    recorders = [(b, n, sources.get(i, "")) for b, n, i in streams("source-outputs", "source")]
    recorders = [r for r in recorders if r[2] and not r[2].endswith(".monitor")]
    if recorders:
        binary, app, mic = recorders[-1]
        why = f"used by {app}"
        players = [(b, monitors.get(i)) for b, _, i in streams("sink-inputs", "sink") if monitors.get(i)]
        same_app = [m for b, m in players if b == binary]
        if same_app:
            spk = same_app[-1]
    return mic, spk, why


def http_json(url, payload=None, timeout=180):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


class Screen:
    """Scrolling final lines with a live footer of one partial line per stream."""

    def __init__(self, streams, log):
        self.streams = streams
        self.partial = {s: "" for s in streams}
        self.hint = {s: "" for s in streams}
        self.live_who = {s: "" for s in streams}  # live guess of who is speaking (footer only)
        self.who_now = {}  # stream -> function giving the voices of the line in progress
        self.log = log
        self.drawn = 0

    def _clear_footer(self):
        if self.drawn:
            sys.stdout.write(f"\033[{self.drawn}F\033[J")
            self.drawn = 0

    def _draw_footer(self):
        width = shutil.get_terminal_size().columns
        for s in self.streams:
            text = self.partial[s] or self.hint[s]
            tag = f"{s} {self.live_who[s]}…" if self.live_who[s] else s
            room = width - len(tag) - 4
            if len(text) > room:
                text = "…" + text[-(room - 1):]
            sys.stdout.write(f"{DIM}{tag}> {text}{RESET}\n")
        self.drawn = len(self.streams)
        sys.stdout.flush()

    def update(self, stream, text):
        self.partial[stream] = text
        self._clear_footer()
        self._draw_footer()

    def who(self, stream, label):
        if self.live_who.get(stream) != label:
            self.live_who[stream] = label
            self._clear_footer()
            self._draw_footer()

    def status(self, stream, text):
        """Footer text that describes state ("speaking…") and is never saved."""
        self.hint[stream] = text
        self._clear_footer()
        self._draw_footer()

    def final(self, stream, text, who=None):
        """who: speaker label for the other side ("A", "B", "?"), shown and saved as "spk A"."""
        self.partial[stream] = ""
        self.live_who[stream] = ""
        self._clear_footer()
        ts = dt.datetime.now().strftime("%H:%M:%S")
        color = COLORS.get(stream, "\033[1m")
        tag = f"{stream} {who}" if who else stream
        sys.stdout.write(f"{DIM}{ts}{RESET} {color}{tag:>5}{RESET}  {text}\n")
        if self.log:
            self.log.write(f"{ts} [{tag}] {text}\n")
            self.log.flush()
        self._draw_footer()

    def flush_partials(self):
        """On exit, keep any sentence still in progress instead of losing it."""
        for s, text in self.partial.items():
            if text:
                who = self.who_now[s]() if s in self.who_now else None
                self.final(s, text + " [cut]", who)

    def info(self, msg):
        self._clear_footer()
        sys.stdout.write(f"{DIM}# {msg}{RESET}\n")
        self._draw_footer()


async def run_stream(name, source, ws_url, screen, gain, turn_detection, speakers=None):
    proc = await asyncio.create_subprocess_exec(
        "parec", "-d", source, "--format=s16le", f"--rate={RATE}", "--channels=1",
        "--latency-msec=50", "--raw",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,  # Ctrl+C reaches us, and we stop parec ourselves
    )
    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            if turn_detection:
                await ws.send(json.dumps({"type": "session.update",
                                          "session": {"turn_detection": turn_detection}}))

            # Speaker labels. Lemonade's speech events arrive too late to cut the audio, so
            # split the other side's audio at short pauses here and end Lemonade's line at
            # the same place (input_audio_buffer.commit): one piece, one line, one voice.
            # Without the commit, a talk with no 0.8 s pause became one unlabelled line.
            seg = Segmenter(SPEAKER_PAUSE_MS) if speakers else None
            pieces = []  # [stream position at piece end, speech ms, label task]
            commits = collections.deque()  # stream positions of commits not answered yet
            pos, line_mark = [0], [0]

            async def peek(pcm, n):
                label = await speakers.label(pcm, peek=True)
                seg.heard(n, label)
                screen.who(name, label)

            def votes_between(since, until):
                votes = {}
                for end, ms, task in pieces:
                    if since < end <= until and task.done():
                        label = task.result()
                        if label != "?":
                            votes[label] = votes.get(label, 0) + ms
                if not votes:
                    return "?"
                total = sum(votes.values())  # a line holding several voices shows all of them
                return "+".join(sorted(v for v, ms in votes.items() if ms >= 0.3 * total))

            async def line_speaker():
                since = line_mark[0]
                line_mark[0] = until = commits.popleft() if commits else pos[0]
                for end, ms, task in pieces:
                    if since < end <= until:
                        await task
                return votes_between(since, until)

            def who_now():  # a line still open at Ctrl+C: the voices heard so far, or the live guess
                who = votes_between(line_mark[0], pos[0])
                return who if who != "?" else screen.live_who[name] or "?"

            if speakers:
                screen.who_now[name] = who_now

            async def send():
                while chunk := await proc.stdout.readexactly(CHUNK_BYTES):
                    if gain != 1.0:
                        chunk = apply_gain(chunk, gain)
                    pos[0] += len(chunk)
                    done = None
                    if seg:
                        done, live = seg.feed(chunk)
                        if live:
                            asyncio.create_task(peek(live, seg.n))
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode(),
                    }))
                    if seg and done:
                        pcm, ms = done
                        pieces.append([pos[0], ms, asyncio.create_task(speakers.label(pcm))])
                        del pieces[:-50]
                        commits.append(pos[0])
                        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

            async def recv():
                async for raw in ws:
                    ev = json.loads(raw)
                    t = ev.get("type", "")
                    if t == "conversation.item.input_audio_transcription.delta":
                        screen.update(name, " ".join(ev.get("delta", "").split()))
                    elif t == "conversation.item.input_audio_transcription.completed":
                        text = " ".join(ev.get("transcript", "").split())
                        if text:
                            screen.final(name, text, await line_speaker() if speakers else None)
                        else:
                            screen.update(name, "")
                    elif t == "input_audio_buffer.cleared" and commits:
                        commits.popleft()  # committed between sentences: nothing to transcribe
                    elif t == "error":
                        screen.info(f"{name}: error {ev.get('error', {}).get('message', ev)}")

            await asyncio.gather(send(), recv())
    finally:
        await stop_parec(proc)


async def stop_parec(proc):
    if proc.returncode is None:
        proc.terminate()
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), 2)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


SPEAKER_RMS = 0.01      # loudness that counts as speech when splitting the other side
SPEAKER_PAUSE_MS = 600  # a pause this long ends a piece and its line (Lemonade's own end: 0.8 s)
SPEAKER_SOFT_S = 10     # after this long without a pause, end the piece at the next dip between words
SPEAKER_MAX_S = 20      # and here at the latest, so long talks keep scrolling


class Segmenter:
    """Splits audio at pauses. feed() returns (finished piece or None, live window or None).

    Talk with no pause (or over background noise) is cut at a dip between words once the
    piece is long, or once the live guess says the voice changed (cut_soon), never mid-word.
    """

    def __init__(self, pause_ms):
        self.pause_ms = pause_ms
        self.buf, self.pre = bytearray(), collections.deque(maxlen=3)
        self.on, self.ms, self.quiet, self.peeked = False, 0, 0, 0
        self.n, self.level, self.cut_soon, self.voice = 0, 0.0, False, ""

    def feed(self, chunk):
        r = rms(chunk)
        loud = r > SPEAKER_RMS
        if loud and not self.on:
            self.on, self.buf, self.ms, self.peeked = True, bytearray(b"".join(self.pre)), 0, 0
            self.n, self.level, self.cut_soon, self.voice = self.n + 1, r, False, ""
        if not self.on:
            self.pre.append(chunk)
            return None, None
        self.buf += chunk
        if loud:
            self.ms += 100
            self.quiet = 0
        else:
            self.quiet += 100
        dip = r < 0.5 * self.level
        self.level = 0.9 * self.level + 0.1 * r
        live = None
        if loud and self.ms >= 1500 and self.ms - self.peeked >= 1500:
            self.peeked = self.ms
            live = bytes(self.buf[-4 * RATE * 2:])
        long = len(self.buf) >= SPEAKER_SOFT_S * RATE * 2
        if (self.quiet >= self.pause_ms or ((long or self.cut_soon) and dip)
                or len(self.buf) >= SPEAKER_MAX_S * RATE * 2):
            self.on = False
            return (bytes(self.buf), self.ms), live
        return None, live

    def heard(self, n, label):
        """Live guess for piece n: a different confident voice than earlier means cut soon."""
        if n != self.n or not self.on or label == "?":
            return
        if self.voice and label != self.voice and self.ms >= 3000:
            self.cut_soon = True
        self.voice = label


def config_value(key):
    if CONFIG.exists():
        for line in CONFIG.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == key and v.strip():
                return v.strip()
    return None


def find_helper(name):
    for p in (Path(__file__).resolve().with_name(name), Path.home() / ".local/share/whispering" / name):
        if p.exists():
            return p
    sys.exit(f"{name} not found next to whispering or in ~/.local/share/whispering/")


class Speakers:
    """Talks to speakers.py, which labels pieces of the other side's audio A, B, C."""

    async def start(self, model_dir):
        self.proc = await asyncio.create_subprocess_exec(
            str(find_helper("speakers.py")), "--model-dir", model_dir,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        self.lock, self.n = asyncio.Lock(), 0
        line = await asyncio.wait_for(self.proc.stdout.readline(), 900)  # first run downloads
        if b"ready" not in line:
            raise RuntimeError("speakers.py did not start")
        return self

    async def label(self, pcm, peek=False):
        async with self.lock:
            self.n += 1
            try:
                req = {"id": self.n, "pcm": base64.b64encode(pcm).decode(), "peek": peek}
                self.proc.stdin.write((json.dumps(req) + "\n").encode())
                await self.proc.stdin.drain()
                return json.loads(await asyncio.wait_for(self.proc.stdout.readline(), 30))["label"]
            except Exception:
                return "?"

    async def stop(self):
        if self.proc.returncode is None:
            try:  # the whole group: `uv run` does not always pass the signal on to python
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(self.proc.wait(), 2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass


def find_khmer_server():
    for p in (Path(__file__).resolve().with_name("khmer_server.py"),
              Path.home() / ".local/share/whispering/khmer_server.py"):
        if p.exists():
            return p
    sys.exit("khmer_server.py not found next to whispering or in ~/.local/share/whispering/")


def start_khmer_server(model):
    """Reuse a running Khmer server, or start one and return its process."""
    url = f"http://127.0.0.1:{KM_PORT}"
    try:
        http_json(url, timeout=2)
        return url, None
    except Exception:
        pass
    logpath = Path.home() / ".cache/whispering/khmer_server.log"
    logpath.parent.mkdir(parents=True, exist_ok=True)
    print("# starting the Khmer model (the first run downloads about 2 GB)…", flush=True)
    proc = subprocess.Popen([str(find_khmer_server()), "--model", model, "--port", str(KM_PORT)],
                            stdout=open(logpath, "w"), stderr=subprocess.STDOUT,
                            start_new_session=True)  # so Ctrl+C doesn't kill it mid-request
    for _ in range(1800):
        try:
            http_json(url, timeout=2)
            return url, proc
        except Exception:
            if proc.poll() is not None:
                sys.exit(f"the Khmer server stopped; see {logpath}")
            time.sleep(1)
    proc.terminate()
    sys.exit(f"the Khmer server did not start in 30 minutes; see {logpath}")


def post_pcm(url, pcm):
    req = urllib.request.Request(f"{url}/transcribe", data=pcm,
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode()).get("text", "")


def rms(chunk):
    a = array.array("h", chunk)
    return math.sqrt(sum(v * v for v in a) / max(len(a), 1)) / 32768


async def run_stream_km(name, source, km_url, screen, gain, pause, threshold, speakers=None):
    """Khmer mode: cut the audio at pauses ourselves and send each piece to khmer_server."""
    proc = await asyncio.create_subprocess_exec(
        "parec", "-d", source, "--format=s16le", f"--rate={RATE}", "--channels=1",
        "--latency-msec=50", "--raw",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,  # Ctrl+C reaches us, and we stop parec ourselves
    )
    queue = asyncio.Queue()

    async def worker():
        while True:
            pcm = await queue.get()
            screen.status(name, "(transcribing…)")
            try:
                text = await asyncio.to_thread(post_pcm, km_url, pcm)
            except asyncio.CancelledError:
                KM_PENDING.append((name, pcm, False))
                raise
            except Exception as e:
                screen.info(f"{name}: Khmer server error: {e}")
                continue
            screen.status(name, "")
            if text:
                screen.final(name, text, await speakers.label(pcm) if speakers else None)

    task = asyncio.create_task(worker())
    preroll = collections.deque(maxlen=3)  # 300 ms kept from before speech starts
    buf, in_speech, speech_ms, silence_ms = bytearray(), False, 0, 0
    try:
        while chunk := await proc.stdout.readexactly(CHUNK_BYTES):
            if gain != 1.0:
                chunk = apply_gain(chunk, gain)
            if rms(chunk) > threshold:
                if not in_speech:
                    in_speech, buf, speech_ms = True, bytearray(b"".join(preroll)), 0
                    screen.status(name, "(speaking…)")
                speech_ms += 100
                silence_ms = 0
            elif in_speech:
                silence_ms += 100
            if not in_speech:
                preroll.append(chunk)
                continue
            buf += chunk
            if silence_ms >= pause * 1000 or len(buf) >= KM_MAX_SECONDS * RATE * 2:
                if speech_ms >= 300:
                    queue.put_nowait(bytes(buf))
                else:
                    screen.status(name, "")
                in_speech, buf, silence_ms = False, bytearray(), 0
    finally:
        await stop_parec(proc)
        task.cancel()
        while not queue.empty():
            KM_PENDING.append((name, queue.get_nowait(), False))
        if in_speech and speech_ms >= 300:
            KM_PENDING.append((name, bytes(buf), True))


def apply_gain(chunk, gain):
    a = array.array("h", chunk)
    for i, v in enumerate(a):
        a[i] = max(-32768, min(32767, int(v * gain)))
    return a.tobytes()


def main():
    mic_default, spk_default, why = default_sources()
    p = argparse.ArgumentParser(description="Live transcription of mic and speaker monitor via Lemonade.")
    p.add_argument("--mic", default=mic_default, help=f"mic source (default: {mic_default})")
    p.add_argument("--spk", default=spk_default, help=f"speaker monitor source (default: {spk_default})")
    p.add_argument("--no-mic", action="store_true", help="only transcribe the speaker monitor")
    p.add_argument("--no-spk", action="store_true", help="only transcribe the mic")
    p.add_argument("-m", "--model", default="Whisper-Large-v3-Turbo", help="Lemonade transcription model")
    p.add_argument("--server", default="http://localhost:13305/api/v1", help="Lemonade REST base URL")
    p.add_argument("-o", "--log", help="append final lines to this file "
                   "(default: ~/transcripts/YYYY-MM-DD_HHMM.txt)")
    p.add_argument("--no-save", action="store_true", help="do not write a transcript file")
    p.add_argument("--mic-gain", type=float, default=1.0, help="multiply mic samples (e.g. 2.0)")
    p.add_argument("--pause", type=float, metavar="SEC",
                   help="silence that ends a line (Lemonade default 0.8)")
    p.add_argument("--threshold", type=float,
                   help="loudness that counts as speech (Lemonade default 0.01); raise if silence produces text")
    p.add_argument("--language", choices=["auto", "km"], default="auto",
                   help="auto: Lemonade's Whisper (English and most languages); km: a local Khmer model")
    p.add_argument("--mic-language", choices=["auto", "km"],
                   help="language for the mic only (default: --language)")
    p.add_argument("--spk-language", choices=["auto", "km"],
                   help="language for the other side only (default: --language)")
    p.add_argument("--km-model", default=os.environ.get("WHISPERING_KM_MODEL", KM_MODEL),
                   help="Khmer model id or local path (env WHISPERING_KM_MODEL)")
    p.add_argument("--speakers", action="store_true",
                   help="label the voices on the other side spk A, B, C (local, CPU); or speakers = on in the config")
    p.add_argument("--summary", action="store_true",
                   help="at the end, summarise without asking (local model, see SUMMARY below)")
    p.add_argument("--summary-model", metavar="NAME",
                   help=f"Lemonade chat model for summaries (default: summary_model in {CONFIG})")
    p.add_argument("--summarize", metavar="TRANSCRIPT", nargs="+",
                   help="summarise existing transcript file(s) and exit")
    p.add_argument("--list", action="store_true", help="list available sources and exit")
    a = p.parse_args()

    if a.list:
        subprocess.run(["pactl", "list", "short", "sources"])
        return
    global SUMMARY_MODEL
    SUMMARY_MODEL = a.summary_model
    if a.summarize:
        paths = [Path(f).expanduser() for f in a.summarize]
        summarize(call_files(paths[0]) if len(paths) == 1 else paths)
        return

    streams = {}  # name -> (source, language)
    if not a.no_mic:
        streams["mic"] = (a.mic, a.mic_language or a.language)
    if not a.no_spk:
        streams["spk"] = (a.spk, a.spk_language or a.language)
    if not streams:
        sys.exit("nothing to transcribe")
    langs = {lang for _, lang in streams.values()}

    ws_url = None
    if "auto" in langs:
        try:
            health = http_json(f"{a.server}/health", timeout=5)
        except Exception as e:
            sys.exit(f"Lemonade not reachable at {a.server}: {e}")
        loaded = [m.get("model_name") for m in health.get("all_models_loaded", [])]
        if a.model not in loaded:
            print(f"# loading {a.model}…", flush=True)
            http_json(f"{a.server}/load", {"model_name": a.model})
        ws_port = health.get("websocket_port", 9000)
        host = a.server.split("//", 1)[1].split("/", 1)[0].split(":")[0]
        ws_url = f"ws://{host}:{ws_port}/realtime?model={a.model}"

    km_url = server = None
    if "km" in langs:
        km_url, server = start_khmer_server(a.km_model)

    turn_detection = {}
    if a.pause is not None:
        turn_detection["silence_duration_ms"] = int(a.pause * 1000)
    if a.threshold is not None:
        turn_detection["threshold"] = a.threshold
    pause = a.pause if a.pause is not None else 0.8
    threshold = a.threshold if a.threshold is not None else 0.01

    log = None
    if not a.no_save:
        path = os.path.expanduser(a.log or f"~/transcripts/{dt.datetime.now():%Y-%m-%d_%H%M}.txt")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        log = open(path, "a")
    screen = Screen(list(streams), log)
    if log:
        screen.info(f"saving to {log.name}")
    for name, (src, lang) in streams.items():
        engine = f"Khmer model {a.km_model} (local, CPU)" if lang == "km" else f"Lemonade {a.model}"
        auto = f" ({why})" if src == {"mic": mic_default, "spk": spk_default}[name] else ""
        screen.info(f"{name}: {src}{auto} -> {engine}")
    if turn_detection:
        screen.info(f"turn detection: {turn_detection}")
    screen.info("Ctrl+C to stop")

    use_speakers = "spk" in streams and (
        a.speakers or (config_value("speakers") or "").lower() in ("on", "yes", "true", "1"))
    model_dir = os.environ.get("WHISPERING_MODEL_DIR") or config_value("model_dir") \
        or str(Path.home() / ".local/share/whispering/models")

    async def go():
        speakers = None
        if use_speakers:
            screen.info(f"speaker labels on (model in {model_dir}; the first run downloads 25 MB)")
            try:
                speakers = await Speakers().start(model_dir)
            except Exception as e:
                screen.info(f"speaker labels off: {e}")
        tasks = []
        for n, (src, lang) in streams.items():
            gain = a.mic_gain if n == "mic" else 1.0
            sp = speakers if n == "spk" else None
            if lang == "km":
                tasks.append(run_stream_km(n, src, km_url, screen, gain, pause, threshold, sp))
            else:
                tasks.append(run_stream(n, src, ws_url, screen, gain, turn_detection, sp))
        try:
            await asyncio.gather(*tasks)
        finally:
            if speakers:
                await speakers.stop()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass
    finally:
        screen.flush_partials()
        for name, pcm, cut in KM_PENDING:
            screen.status(name, "(finishing…)")
            try:
                text = post_pcm(km_url, pcm)
            except Exception as e:
                screen.info(f"{name}: Khmer server error: {e}")
                continue
            screen.status(name, "")
            if text:
                screen.final(name, text + (" [cut]" if cut else ""))
        if log:
            log.close()
        if server:
            server.terminate()
        print()
    if log and os.path.getsize(log.name):
        offer_summary(Path(log.name), a.summary)


# ---------------------------------------------------------------- SUMMARY
# At the end of a run: s = summarise with a Lemonade chat model, q or Ctrl+C = quit.
# The model is a per-person setting (--summary-model, WHISPERING_SUMMARY_MODEL,
# or "summary_model = <name>" in ~/.config/whispering/config). whispering never
# unloads anything; it refuses to load a model that doesn't fit in free memory.

LEMONADE_API = "http://localhost:13305/api/v1"
CONFIG = Path.home() / ".config/whispering/config"
SAME_CALL_GAP_MIN = 20  # an earlier file ending this close is part of the same call
NAME = __import__("re").compile(r"^(\d{4}-\d\d-\d\d)_(\d{4})(?:_[a-z0-9-]+)?\.txt$")

SUMMARY_PROMPT = """You summarise a call transcript recorded by a live transcriber.
Each line is "HH:MM:SS [mic|spk] text". [mic] is the user. [spk] is everyone on the other side of the call. When speaker labels are on, [spk A], [spk B] … tell apart voices on the other side ("?" = unknown, "B+C" = several voices in one line); letters are not names, and a name in the text (someone addressed by name) is the only way to know who a letter is.

Write the summary in English Markdown with exactly these sections:

# <Topic>, <date>
## Overview (first sentence: what the call was about and its main outcome; then who was on it)
## Topics discussed (one ### per topic, a short paragraph then bullets)
## Decisions (bullets)
## Action items (one flat checklist, each item as "- [ ] ..."; no owners, no names, no grouping)
## Open questions

Rules, more important than style:
- Use only what the transcript says. Never invent names, dates, numbers, owners or decisions.
- Spell names as they appear. If a name is uncertain, say so.
- Do not say who will do an action item. Write each one as the task itself ("Reply to the supplier's last email introducing the new supply chain manager"), without a person's name or "Jeff will". Who does what is checked by a person afterwards.
- [spk] is several people: don't attribute a [spk] statement to one person unless a name in the text makes it clear.
- Ignore transcription noise: repeated "Thank you.", ".", "...", sound tags like *Loud noise*, stray words in another language, a phrase looping.
- Leave out private side conversations that are not part of the call.
- Never use the em-dash character. Use commas, colons or full stops.

After the summary, on the very last line, write a topic slug for file names: "SLUG: " followed by 2 to 4 lowercase words joined by hyphens.
"""


def offer_summary(path, auto):
    if not auto:
        if not sys.stdin.isatty():
            return
        print(f"{DIM}# s = summarise this call, q or Ctrl+C = quit{RESET}", flush=True)
        if read_key() != "s":
            return
    summarize(call_files(path))


def read_key():
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)  # keeps Ctrl+C working
        return sys.stdin.read(1).lower()
    except KeyboardInterrupt:
        return "q"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def span(path):
    """First and last HH:MM:SS of a transcript."""
    times = [line[:8] for line in path.read_text(errors="replace").splitlines()
             if len(line) > 10 and line[2] == ":" and line[8] == " "]
    return (times[0], times[-1]) if times else (None, None)


def call_files(path):
    """This transcript plus the earlier ones of the same call (a restart mid-call)."""
    m = NAME.match(path.name)
    if not m:
        return [path]
    day = m.group(1)
    earlier = sorted(f for f in path.parent.glob(f"{day}_*.txt") if NAME.match(f.name) and f.name < path.name)
    files = [path]
    for f in reversed(earlier):
        start = span(files[0])[0]
        end = span(f)[1]
        if not start or not end:
            break
        gap = (dt.datetime.strptime(start, "%H:%M:%S") - dt.datetime.strptime(end, "%H:%M:%S")).total_seconds()
        if 0 <= gap <= SAME_CALL_GAP_MIN * 60:
            files.insert(0, f)
        else:
            break
    return files


SUMMARY_MODEL = None  # set from --summary-model in main()


def summary_model():
    if SUMMARY_MODEL:
        return SUMMARY_MODEL
    if os.environ.get("WHISPERING_SUMMARY_MODEL"):
        return os.environ["WHISPERING_SUMMARY_MODEL"]
    if CONFIG.exists():
        for line in CONFIG.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "summary_model" and value.strip():
                return value.strip()
    return None


def pick_llm():
    """The configured Lemonade model, if it is loaded or fits in free memory."""
    model = summary_model()
    if not model:
        print("# no summary model set. Pick a Lemonade chat model (`lemonade list`) and add\n"
              f"#   summary_model = <model name>\n# to {CONFIG}, or use --summary-model <name>")
        return None
    try:
        health = http_json(f"{LEMONADE_API}/health", timeout=3)
        if model not in [m.get("model_name") for m in health.get("all_models_loaded", [])]:
            info = next((m for m in http_json(f"{LEMONADE_API}/models", timeout=5)["data"] if m["id"] == model), None)
            if not info or not info.get("downloaded"):
                print(f"# summary model {model} is not downloaded in Lemonade (`lemonade pull {model}`)")
                return None
            need = (info.get("size") or 0) + 8
            avail = int(next(l.split()[1] for l in open("/proc/meminfo") if l.startswith("MemAvailable"))) / 2**20
            if avail < need:
                print(f"# not enough free memory to load {model}: {avail:.0f} GB free, needs about {need:.0f} GB")
                return None
        return LEMONADE_API, model, model
    except Exception as e:
        print(f"# Lemonade not reachable for the summary: {e}")
        return None


def summarize(files):
    files = [f for f in files if f.exists()]
    if not files:
        sys.exit("no transcript to summarise")
    llm = pick_llm()
    if not llm:
        print(f"# to summarise later: whispering --summarize {' '.join(str(f) for f in files)}")
        return
    url, model, label = llm
    day = NAME.match(files[0].name).group(1) if NAME.match(files[0].name) else dt.date.today().isoformat()
    text = "\n\n".join(f"### File {f.name}\n{f.read_text(errors='replace')}" for f in files)
    body = {"model": model, "temperature": 0.2, "max_tokens": 6000,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": SUMMARY_PROMPT},
                         {"role": "user", "content": f"Date: {day}\n\n{text}"}]}
    print(f"# summarising {len(files)} file(s) with {label}…", flush=True)
    t = time.time()
    req = urllib.request.Request(f"{url}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            out = json.loads(r.read())["choices"][0]["message"].get("content") or ""
    except Exception as e:
        print(f"# summary failed: {e}")
        return
    import re
    slug = "call"
    m = re.search(r"^\s*SLUG:\s*(.+?)\s*$", out, re.M)
    if m:
        words = re.sub(r"[^a-z0-9]+", "-", m.group(1).lower()).strip("-").split("-")
        slug = "-".join(w for w in words if w)[:40].strip("-") or "call"
        out = out[:m.start()].rstrip() + "\n"
    out = out.replace(" \u2014 ", ", ").replace("\u2014", ", ")

    renamed = []
    for f in files:
        mm = NAME.match(f.name)
        if mm and f.name == f"{mm.group(1)}_{mm.group(2)}.txt":
            new = f.with_name(f"{mm.group(1)}_{mm.group(2)}_{slug}.txt")
            if not new.exists():
                f.rename(new)
                f = new
        renamed.append(f)
    spans = [f"`{f.name}` ({span(f)[0][:5]} to {span(f)[1][:5]})" for f in renamed if span(f)[0]]
    source = (f"Source: {' and '.join(spans)}. Whisper transcript; names spelled as heard.\n"
              f"Draft by a local model ({label}): check names and action items before sending.\n")
    lines = out.strip().splitlines()
    if lines and lines[0].startswith("# "):
        out = lines[0] + "\n\n" + source + "\n" + "\n".join(lines[1:]).strip() + "\n"
    else:
        out = source + "\n" + out
    target = files[0].parent / f"{day}_{slug}_summary.md"
    if target.exists():
        target = files[0].parent / f"{day}_{NAME.match(renamed[0].name).group(2) if NAME.match(renamed[0].name) else 'x'}_{slug}_summary.md"
    n = 2
    while target.exists():
        target = target.with_name(f"{day}_{slug}_summary-{n}.md")
        n += 1
    target.write_text(out)
    print(f"# summary ({time.time() - t:.0f}s): {target}")
    for old, new in zip(files, renamed):
        if old != new:
            print(f"# renamed {old.name} -> {new.name}")


if __name__ == "__main__":
    main()

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


def default_sources():
    def get(cmd):
        return subprocess.run(["pactl", cmd], capture_output=True, text=True).stdout.strip()

    return get("get-default-source"), get("get-default-sink") + ".monitor"


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
            room = width - len(s) - 4
            if len(text) > room:
                text = "…" + text[-(room - 1):]
            sys.stdout.write(f"{DIM}{s}> {text}{RESET}\n")
        self.drawn = len(self.streams)
        sys.stdout.flush()

    def update(self, stream, text):
        self.partial[stream] = text
        self._clear_footer()
        self._draw_footer()

    def status(self, stream, text):
        """Footer text that describes state ("speaking…") and is never saved."""
        self.hint[stream] = text
        self._clear_footer()
        self._draw_footer()

    def final(self, stream, text):
        self.partial[stream] = ""
        self._clear_footer()
        ts = dt.datetime.now().strftime("%H:%M:%S")
        color = COLORS.get(stream, "\033[1m")
        sys.stdout.write(f"{DIM}{ts}{RESET} {color}{stream:>3}{RESET}  {text}\n")
        if self.log:
            self.log.write(f"{ts} [{stream}] {text}\n")
            self.log.flush()
        self._draw_footer()

    def flush_partials(self):
        """On exit, keep any sentence still in progress instead of losing it."""
        for s, text in self.partial.items():
            if text:
                self.final(s, text + " [cut]")

    def info(self, msg):
        self._clear_footer()
        sys.stdout.write(f"{DIM}# {msg}{RESET}\n")
        self._draw_footer()


async def run_stream(name, source, ws_url, screen, gain, turn_detection):
    proc = await asyncio.create_subprocess_exec(
        "parec", "-d", source, "--format=s16le", f"--rate={RATE}", "--channels=1",
        "--latency-msec=50", "--raw",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            if turn_detection:
                await ws.send(json.dumps({"type": "session.update",
                                          "session": {"turn_detection": turn_detection}}))

            async def send():
                while chunk := await proc.stdout.readexactly(CHUNK_BYTES):
                    if gain != 1.0:
                        chunk = apply_gain(chunk, gain)
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode(),
                    }))

            async def recv():
                async for raw in ws:
                    ev = json.loads(raw)
                    t = ev.get("type", "")
                    if t == "conversation.item.input_audio_transcription.delta":
                        screen.update(name, " ".join(ev.get("delta", "").split()))
                    elif t == "conversation.item.input_audio_transcription.completed":
                        text = " ".join(ev.get("transcript", "").split())
                        if text:
                            screen.final(name, text)
                        else:
                            screen.update(name, "")
                    elif t == "error":
                        screen.info(f"{name}: error {ev.get('error', {}).get('message', ev)}")

            await asyncio.gather(send(), recv())
    finally:
        if proc.returncode is None:
            proc.terminate()


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


async def run_stream_km(name, source, km_url, screen, gain, pause, threshold):
    """Khmer mode: cut the audio at pauses ourselves and send each piece to khmer_server."""
    proc = await asyncio.create_subprocess_exec(
        "parec", "-d", source, "--format=s16le", f"--rate={RATE}", "--channels=1",
        "--latency-msec=50", "--raw",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
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
                screen.final(name, text)

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
        if proc.returncode is None:
            proc.terminate()
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
    mic_default, spk_default = default_sources()
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
    p.add_argument("--list", action="store_true", help="list available sources and exit")
    a = p.parse_args()

    if a.list:
        subprocess.run(["pactl", "list", "short", "sources"])
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
        screen.info(f"{name}: {src} -> {engine}")
    if turn_detection:
        screen.info(f"turn detection: {turn_detection}")
    screen.info("Ctrl+C to stop")

    async def go():
        tasks = []
        for n, (src, lang) in streams.items():
            gain = a.mic_gain if n == "mic" else 1.0
            if lang == "km":
                tasks.append(run_stream_km(n, src, km_url, screen, gain, pause, threshold))
            else:
                tasks.append(run_stream(n, src, ws_url, screen, gain, turn_detection))
        await asyncio.gather(*tasks)

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


if __name__ == "__main__":
    main()

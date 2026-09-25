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
import asyncio
import base64
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import urllib.request

import websockets

RATE = 16000
CHUNK_BYTES = RATE * 2 // 10  # 100 ms of s16le mono

COLORS = {"mic": "\033[1;36m", "spk": "\033[1;33m"}
RESET, DIM = "\033[0m", "\033[2m"


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
        self.log = log
        self.drawn = 0

    def _clear_footer(self):
        if self.drawn:
            sys.stdout.write(f"\033[{self.drawn}F\033[J")
            self.drawn = 0

    def _draw_footer(self):
        width = shutil.get_terminal_size().columns
        for s in self.streams:
            text = self.partial[s]
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


def apply_gain(chunk, gain):
    import array
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
    p.add_argument("--list", action="store_true", help="list available sources and exit")
    a = p.parse_args()

    if a.list:
        subprocess.run(["pactl", "list", "short", "sources"])
        return

    streams = {}
    if not a.no_mic:
        streams["mic"] = a.mic
    if not a.no_spk:
        streams["spk"] = a.spk
    if not streams:
        sys.exit("nothing to transcribe")

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

    turn_detection = {}
    if a.pause is not None:
        turn_detection["silence_duration_ms"] = int(a.pause * 1000)
    if a.threshold is not None:
        turn_detection["threshold"] = a.threshold

    log = None
    if not a.no_save:
        path = os.path.expanduser(a.log or f"~/transcripts/{dt.datetime.now():%Y-%m-%d_%H%M}.txt")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        log = open(path, "a")
    screen = Screen(list(streams), log)
    if log:
        screen.info(f"saving to {log.name}")
    for name, src in streams.items():
        screen.info(f"{name}: {src}")
    screen.info(f"model {a.model}, Ctrl+C to stop")
    if turn_detection:
        screen.info(f"turn detection: {turn_detection}")

    async def go():
        await asyncio.gather(*(
            run_stream(n, s, ws_url, screen, a.mic_gain if n == "mic" else 1.0, turn_detection)
            for n, s in streams.items()
        ))

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass
    finally:
        screen.flush_partials()
        if log:
            log.close()
        print()


if __name__ == "__main__":
    main()

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["torch", "transformers", "numpy", "silero-vad"]
# [tool.uv.sources]
# torch = { index = "pytorch-cpu" }
# [[tool.uv.index]]
# name = "pytorch-cpu"
# url = "https://download.pytorch.org/whl/cpu"
# explicit = true
# ///
"""Local Khmer speech-to-text server for `whispering --language km`.

Runs the Khmer fine-tune of Whisper large-v3-turbo on the CPU and answers
POST /transcribe (body: 16 kHz mono s16le PCM) with {"text": "..."}.
Clips with no voice (Silero VAD) return an empty text instead of a guess.
Listens on 127.0.0.1 only.
"""

import argparse
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import torch
from silero_vad import get_speech_timestamps, load_silero_vad
from transformers import pipeline

MODEL = "1morecupofhottea/whisper-turbo-khmer-v9"
REVISION = "f8644583afbf77707a0fc75eef36f80d116eeb1f"
RATE = 16000

KHMER = "ក-៿᧠-᧿"


def clean(text):
    """The model writes words separated by spaces and phrases by ';'.
    Join Khmer words back together, keep a space between phrases, and
    collapse a phrase repeated three or more times in a row (a decoding loop)."""
    text = text.replace("\ufffd", "")  # half-decoded character at a cut
    text = re.sub(r"(\S+(?: \S+){0,5}?)(?: \1){2,}", r"\1", text)
    text = text.replace(";", "\0")
    text = re.sub(rf"(?<=[{KHMER}]) +(?=[{KHMER}])", "", text)
    return " ".join(text.replace("\0", " ").split())


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", default=MODEL, help="model id or local path")
    p.add_argument("--port", type=int, default=13399)
    p.add_argument("--threads", type=int, default=0, help="CPU threads (default: half the logical cores)")
    a = p.parse_args()

    torch.set_num_threads(a.threads or max(1, (os.cpu_count() or 2) // 2))  # physical cores
    kwargs = {} if "/" not in a.model or a.model.startswith("/") else {"revision": REVISION}
    asr = pipeline("automatic-speech-recognition", model=a.model, device="cpu", **kwargs)
    vad = load_silero_vad()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(200, {"status": "ok", "model": a.model})

        def do_POST(self):
            if self.path != "/transcribe":
                return self.reply(404, {"error": "POST /transcribe"})
            pcm = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
            with lock:
                if not get_speech_timestamps(torch.from_numpy(audio), vad, sampling_rate=RATE):
                    return self.reply(200, {"text": "", "speech": False})
                text = asr({"raw": audio, "sampling_rate": RATE})["text"]
            self.reply(200, {"text": clean(text), "speech": True})

    print(f"khmer_server ready on 127.0.0.1:{a.port} ({a.model})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

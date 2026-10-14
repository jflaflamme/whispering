#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["sherpa-onnx", "numpy"]
# ///
"""Speaker labels for `whispering --speakers`: tells the voices on the other side apart.

Reads one JSON request per line on stdin, {"id": n, "pcm": "<base64 s16le 16 kHz mono>"}
(add "peek": true to only compare, without adding a new voice or updating one),
and answers one JSON line per request on stdout, {"id": n, "label": "A", "score": 0.71}.
Each piece of audio gets a voiceprint (3D-Speaker ERes2Net via sherpa-onnx); pieces whose
voiceprint is close enough to an earlier voice get that voice's letter, otherwise a new
letter. Pieces too short to fingerprint get "?". Everything runs locally on the CPU.
"""

import argparse
import base64
import json
import sys
import urllib.request
from pathlib import Path

import numpy as np
import sherpa_onnx

MODEL = "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{MODEL}"
RATE = 16000
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model-dir", required=True)
    p.add_argument("--threshold", type=float, default=0.4,
                   help="similarity to count as the same voice (tested: 0.4)")
    p.add_argument("--min-seconds", type=float, default=1.0,
                   help="shorter pieces are not fingerprinted at all and get '?'")
    p.add_argument("--new-voice-seconds", type=float, default=2.5,
                   help="only pieces at least this long may start a new voice; shorter ones only match")
    a = p.parse_args()

    model = Path(a.model_dir).expanduser() / MODEL
    if not model.exists():
        model.parent.mkdir(parents=True, exist_ok=True)
        tmp = model.with_suffix(".part")
        urllib.request.urlretrieve(URL, tmp)
        tmp.rename(model)
    ext = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=2))
    voices = []  # summed voiceprints, one per letter
    print(json.dumps({"ready": True}), flush=True)

    for line in sys.stdin:
        req = json.loads(line)
        pcm = np.frombuffer(base64.b64decode(req["pcm"]), dtype=np.int16).astype(np.float32) / 32768
        label, score = "?", None
        if len(pcm) >= a.min_seconds * RATE:
            s = ext.create_stream()
            s.accept_waveform(RATE, pcm)
            s.input_finished()
            v = np.array(ext.compute(s))
            v /= np.linalg.norm(v)
            sims = [float(v @ (c / np.linalg.norm(c))) for c in voices]
            if req.get("peek"):
                if sims and max(sims) >= a.threshold:
                    label, score = LETTERS[int(np.argmax(sims))], max(sims)
                print(json.dumps({"id": req["id"], "label": label, "score": score}), flush=True)
                continue
            if sims and max(sims) >= a.threshold:
                k = int(np.argmax(sims))
                voices[k] += v
                score = sims[k]
            elif len(pcm) < a.new_voice_seconds * RATE:
                print(json.dumps({"id": req["id"], "label": "?", "score": max(sims) if sims else None}), flush=True)
                continue
            elif len(voices) < len(LETTERS):
                k = len(voices)
                voices.append(v.copy())
            else:
                k = int(np.argmax(sims))
            label = LETTERS[k]
        print(json.dumps({"id": req["id"], "label": label, "score": score}), flush=True)


if __name__ == "__main__":
    main()

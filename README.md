# whispering

Live transcription of your microphone and your speakers (the other side of a call) in the terminal, fully local, using [Lemonade](https://github.com/lemonade-sdk/lemonade) and Whisper.

```
10:02:11 spk  Can everyone see my screen?
10:02:15 mic  Yes, go ahead.
mic>
spk> So the first thing on the agenda is
```

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/jflaflamme/whispering/main/install.sh | sh
```

The installer puts `whispering` in `~/.local/bin`, installs `uv` if it's missing, checks that Lemonade is running, and downloads the `Whisper-Large-v3-Turbo` model (about 1.5 GB) if it isn't already there. It does not install Lemonade itself (that needs `sudo`), but tells you how if it's missing.

## Requirements

- Linux with PipeWire or PulseAudio (`parec`, from `pulseaudio-utils`).
- Lemonade Server from the `lemonade-team/stable` PPA, running on port 13305. Tested with 11.9.0 on Ubuntu 26.04. Ubuntu's own archive has an older 10.2.0, which has not been tested.

Check and, if needed, install or update Lemonade:

```sh
apt-cache policy lemonade-server   # want 11.9.0 or newer, from the PPA
lemonade status                    # "Server is running on port 13305"

sudo add-apt-repository ppa:lemonade-team/stable
sudo apt update && sudo apt install lemonade-server
sudo systemctl restart lemond
```

## Use

```sh
whispering                   # mic + speakers, default devices
whispering -o meeting.txt    # save to a chosen file
whispering --no-save         # screen only
whispering --no-mic          # only the other side
whispering --no-spk          # only your mic
whispering --list            # list audio sources
whispering --mic <source> --spk <sink>.monitor
whispering -m Whisper-Tiny   # faster, less accurate
whispering --mic-gain 2      # boost a quiet mic
whispering --pause 1.5       # longer pause before a line ends (default 0.8 s)
whispering --threshold 0.03  # louder sound needed to count as speech (default 0.01)
whispering --language km     # Khmer, with a local Khmer model (see below)
whispering --summary         # summarise at the end without asking
whispering --summarize FILE  # summarise an existing transcript
whispering --spk-language km # you in English, the other side in Khmer
```

- Finished lines scroll with a timestamp, `mic` in cyan and `spk` in yellow. The grey lines at the bottom show text still being recognised. A line is finalised when the speaker pauses for 0.8 s (change with `--pause`).
- Every finished line is saved as it happens to `~/transcripts/YYYY-MM-DD_HHMM.txt` (format `HH:MM:SS [mic|spk] text`). The path is printed at startup.
- Press Ctrl+C to stop. An unfinished sentence is saved with `[cut]` on the end.
- The mic and speaker side are the ones your call app is using (for example the headset your browser call records from), found at startup. With no call app recording, the system defaults are used. Restart after switching headset or output.

## Summary at the end of a call

After Ctrl+C, whispering offers `s = summarise this call, q or Ctrl+C = quit`. With `s`, a local chat model in Lemonade writes a Markdown summary (overview, topics, decisions, action items, open questions), and the transcript and summary are renamed by topic:

```
2026-10-07_1539_stove-pilot.txt
2026-10-07_stove-pilot_summary.md
```

If whispering was restarted during the call, the earlier file (ending less than 20 minutes before) is included. `whispering --summarize <file>` does the same for an existing transcript.

Pick the model once, in `~/.config/whispering/config`:

```
summary_model = <Lemonade model name>
```

or per run with `--summary-model <name>`. Use a model that fits your memory: whispering checks free memory before loading it and refuses if it doesn't fit, and never unloads anything. In our tests on a 22-minute call, large models (Qwen3.6-35B-A3B, about 30 s; Qwen3.8-27B, about 90 s) gave usable drafts, and small ones (1 to 8B) invented facts and people. A GGUF you already have outside Lemonade can be used without downloading it again: `lemonade config set extra_models_dir=/path/to/folder`, then use the `extra.<name>` it lists.

The summary is a draft by a local model: check it before sending. Action items are listed without owners on purpose, because local models often guess wrong about who will do what.

## Khmer

Lemonade's Whisper cannot transcribe Khmer: it answers with invented English, or with mixed scripts when Khmer is forced. For Khmer calls, use:

```sh
whispering --language km
```

This switches both streams to a local Khmer model, [whisper-turbo-khmer-v9](https://huggingface.co/1morecupofhottea/whisper-turbo-khmer-v9) (MIT), a fine-tune of Whisper large-v3-turbo. It runs on the CPU in a small helper, `khmer_server.py`, which `whispering` starts on its own and stops when you quit. Nothing leaves the laptop. Lemonade is not used in this mode.

- **First run:** downloads PyTorch (CPU) and the model, about 2 GB in total, so allow a few minutes. Later starts take a few seconds.
- **Lines:** `whispering` cuts the audio at pauses (0.8 s, change with `--pause`) and sends each piece to the model. There is no live grey preview in this mode; the footer shows `(speaking…)` and `(transcribing…)` instead. Pieces with no voice are dropped, so silence does not produce text.
- **Accuracy:** on 100 random clips of the FLEURS Khmer test set, about **13% of characters wrong**, against 90 to 102% for Lemonade's Whisper. FLEURS is clearly read speech; real calls (fillers, overlapping voices, phone audio) come out noticeably worse, and names and English words inside Khmer speech are often mangled.
- **Speed:** about 4 to 5 times faster than real time on a Ryzen AI Max laptop (16 cores). Slower CPUs may fall behind on long calls; `--threads N` on `khmer_server.py` sets the core count.
- **Model location:** the model downloads to the Hugging Face cache. To use a copy already on disk: `WHISPERING_KM_MODEL=/path/to/whisper-turbo-khmer-v9 whispering --language km`.
- **One language per side:** each side uses one engine for the whole run. When you speak English and the other side speaks Khmer, use `whispering --spk-language km` (your mic stays on Lemonade's Whisper). `--mic-language km` does the opposite, and `--language km` makes both sides Khmer.
- **Mixed speech is not handled:** the Khmer model writes English words in Khmer letters, and Lemonade's Whisper turns Khmer into invented English. A sentence that switches language comes out wrong on either engine.

### Testing the Khmer branch

Khmer support is on the `khmer` branch until it has been tested. Either install from the branch:

```sh
curl -fsSL https://raw.githubusercontent.com/jflaflamme/whispering/khmer/install.sh | WHISPERING_BRANCH=khmer sh
```

or run it from a checkout:

```sh
git clone https://github.com/jflaflamme/whispering && cd whispering
git checkout khmer        # or: git pull, on an existing checkout
./whispering.py --language km
```

## Stop invented text in silence (recommended)

Lemonade decides "someone is speaking" by volume alone, so background noise reaches Whisper, which then invents lines such as "Thank you.". Turning on whisper.cpp's Silero voice detector fixes this: clips with no voice are skipped. It is a Lemonade server setting, so it applies to every app using Lemonade's Whisper.

```sh
D=/var/lib/lemonade/whisper-vad
sudo mkdir -p $D
sudo curl -fsSL -o $D/ggml-silero-v6.2.0.bin https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin
sudo chown -R lemonade: $D
lemonade config set whispercpp.args="--vad --vad-model $D/ggml-silero-v6.2.0.bin --suppress-nst"
# reload Whisper so the setting takes effect
curl -s localhost:13305/api/v1/unload -H 'Content-Type: application/json' -d '{"model_name":"Whisper-Large-v3-Turbo"}'
```

`/var/lib/lemonade` is the home of the `lemonade` service user on the PPA install; the model file just has to be readable by that user. Check it took effect with `pgrep -af whisper-server` (the line should include `--vad`) after the next transcription starts.


- Without the Silero setting above, Whisper invents text such as "Thank you." when a source is silent, mostly on `mic`.
- There is no echo cancellation: use headphones, or the other side will also appear under `mic`.
- Everyone on the far end shares the `spk` label.

## Troubleshooting

- **`Lemonade not reachable`:** the server isn't running. Try `sudo systemctl restart lemond`, then `lemonade status`.
- **Model missing:** `lemonade pull Whisper-Large-v3-Turbo`.
- **Runs but no text:** check `whispering --list` shows the sources you expect, and that your mic isn't muted (`wpctl status`).

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
```

- Finished lines scroll with a timestamp, `mic` in cyan and `spk` in yellow. The grey lines at the bottom show text still being recognised. A line is finalised when the speaker pauses.
- Every finished line is saved as it happens to `~/transcripts/YYYY-MM-DD_HHMM.txt` (format `HH:MM:SS [mic|spk] text`). The path is printed at startup.
- Press Ctrl+C to stop. An unfinished sentence is saved with `[cut]` on the end.
- Default devices are read at startup: restart after switching headset or output.

## Good to know

- Whisper invents text such as "Thank you." or "..." when a source is silent. Expect a few of those lines, mostly on `mic`.
- There is no echo cancellation: use headphones, or the other side will also appear under `mic`.
- Everyone on the far end shares the `spk` label.

## Troubleshooting

- **`Lemonade not reachable`:** the server isn't running. Try `sudo systemctl restart lemond`, then `lemonade status`.
- **Model missing:** `lemonade pull Whisper-Large-v3-Turbo`.
- **Runs but no text:** check `whispering --list` shows the sources you expect, and that your mic isn't muted (`wpctl status`).

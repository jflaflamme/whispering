---
name: whispering
description: Find, read and summarise call and meeting transcripts recorded by the local `whispering` CLI (files in ~/transcripts), then rename the transcripts and summary by topic. Use when the user asks for the transcript or summary of a call or meeting ("summarise my last call", "summary of today's meetings", "what did we decide with X", "action points from the call"), wants several transcripts of one call combined, or asks whether a call is still being recorded. Also reports disk use and cleans up old audio on request, and knows how to start whispering (English, Khmer, per-side language, speaker labels, meeting rooms) and its known transcription quirks.
---

# whispering: call transcripts and summaries

`whispering` (https://github.com/jflaflamme/whispering) live-transcribes the mic and the speaker side of a call with local models. It writes one transcript file per run to `~/transcripts/`. This skill turns those files into summaries.

## Files

- Transcript: `~/transcripts/YYYY-MM-DD_HHMM.txt` (time = when that run started), one line per finished sentence: `HH:MM:SS [mic|spk] text`. `mic` = the user's microphone, `spk` = everyone on the other side of a call.
- With speaker labels on (`--speakers`), lines read `[spk A]`, `[spk B]`, or `[mic A]`, `[mic B]` when the mic sits in a meeting room. Letters are not names and only hold within one run. `?` = too short to recognise, `B+C` = two voices in one line. Each side has its own letters: `mic A` and `spk A` are different people.
- A call often spans several files: whispering was restarted mid-call (device change, a fix), so consecutive files minutes apart belong to the same call.
- Files renamed by topic: `YYYY-MM-DD_HHMM_<slug>.txt`, summaries `YYYY-MM-DD_<slug>_summary.md`. Each transcript usually has its audio next to it (`.mic.opus` / `.spk.opus`, or `.wav` if ffmpeg was missing or the run was cut off); it follows the transcript when renamed.
- Summaries are local Markdown files next to the transcripts.

## Step 1: find the transcripts

Run the listing script from this skill's folder:

```sh
python3 -I <this skill's folder>/scripts/sessions.py                    # all
python3 -I <this skill's folder>/scripts/sessions.py --date 2026-10-07  # one day
```

It prints one row per file, grouped into `call N` (files less than 20 min apart; `--gap` changes that), with time span, mic/spk line counts, how many mic lines are real speech, `RECORDING NOW` when a running whispering still has the file open, and which summary already mentions it. It is read-only.

- "Last call" = the highest call number. Confirm which files you are summarising in one line before writing.
- If a file is `RECORDING NOW`, the call is not over: say so and ask whether to summarise what exists so far. Never stop or restart whispering yourself, and never touch audio devices or the Lemonade server while the user may be on a call.
- Very few real mic lines against many spk lines usually means whispering recorded the wrong mic: say so in the summary's caveats.

## Step 2: read and summarise

Read every file of the call in full, in time order. Write the summary in the user's language (English by default) with this shape:

```
# <Topic>, <D Mon YYYY>

Source: `<file1>` (HH:MM to HH:MM) and `<file2>` (...). Whisper transcript; names spelled as heard. <gaps between files, missing start>

## Overview          lead sentence = what the call was about + its main outcome; who was on it
## Topics discussed  one ### per topic, a short lead then bullets
## Decisions         bullets, the decided thing in bold
## Action items      grouped by owner (**<name>**, **Owner not stated**), as - [ ] tasks; "Done during the call:" for things finished live
## Open questions
## Transcript caveats
```

Rules (they matter more than polish):

- **Never invent facts.** Names exactly as heard; when a name is uncertain, say so. Only give an action item an owner when the transcript says who will do it; otherwise "Owner not stated". No dates, amounts or decisions that the transcript doesn't contain. A date someone says ("next Wednesday") stays as said; don't compute it.
- `spk` without a letter is several people: don't attribute a far-side statement to one person unless the transcript makes it clear ("Sam, can you…", a name in the sentence). A speaker letter is a voice, not a name: link a letter to a name only when the transcript shows it (someone answers right after being addressed by name), and say it is inferred.
- Ignore Whisper fillers and inventions: "Thank you." runs, ".", "…", `*Loud noise*`, stray lines in another language or script, a loop repeating one phrase. Mention in caveats if they were heavy.
- Leave out private side conversations picked up by the mic that aren't part of the call, and say that something was left out (not what).

## Step 3: rename by topic

After the summary is written, give the call a short topic slug (2 to 4 lowercase words, hyphenated, e.g. `budget-review`, `supplier-pilot`) and rename:

- each transcript: `YYYY-MM-DD_HHMM.txt` -> `YYYY-MM-DD_HHMM_<slug>.txt` (keep the date and time, they identify the part), and its `.mic.opus` / `.spk.opus` / `.wav` audio if present
- the summary: `YYYY-MM-DD_<slug>_summary.md` (if two calls on the same day share a slug, add the start time: `YYYY-MM-DD_HHMM_<slug>_summary.md`)

Then update the `Source:` line in the summary to the new transcript names.

Safety:
- Never rename a file that `sessions.py` shows as `RECORDING NOW`.
- Never overwrite: if the target name exists, stop and ask.
- Use `mv -n` and check the result. Don't rename anything outside `~/transcripts/`.
- Tell the user the old and new names.

## Step 4: hand off

If the call belongs to a project with its own folder, offer to copy the summary there as `CALL_SUMMARY_YYYY-MM-DD.md` (never overwrite; if it exists, ask), with a short "Next step" section if work was started during the call.

## Housekeeping

When the user asks how much space transcripts take, or to clean up old recordings:

```sh
python3 -I <this skill's folder>/scripts/housekeeping.py                        # report: per month, count and size
python3 -I <this skill's folder>/scripts/housekeeping.py --compress             # show which WAV files would be compressed
python3 -I <this skill's folder>/scripts/housekeeping.py --delete-audio --days 90   # show which audio would be deleted
```

- Without `--yes` the script changes nothing. Show the user what would happen, and add `--yes` only after they confirm. Deleting audio cannot be undone.
- Only audio (`.opus`, `.wav`) is ever deleted, never transcripts or summaries. Files a running whispering is still writing are skipped.
- WAV files are left when a run was cut off or ffmpeg was missing; `--compress` turns them into Opus (about a tenth of the size).
- Offer it, don't do it on your own: suggest it when the folder is large or old audio piles up.

## Starting whispering (for reference)

```sh
whispering                              # a call: your mic + the other side, English (Lemonade Whisper)
whispering --speakers                   # also label the voices on the other side: spk A, spk B, ...
whispering --no-spk --speakers          # a meeting room: the laptop mic only, voices labelled mic A, mic B, ...
whispering --spk-language km            # you in English, the other side in Khmer
whispering --language km                # everyone in Khmer
whispering --mic <source>               # force a mic (see `whispering --list`)
whispering --summarize FILE...          # summarise existing transcripts with the local model
```

## Quirks to know when reading transcripts

- **Long turns:** without speaker labels, a line ends at a 0.8 s pause, so long unbroken turns become one huge line; the order of points inside it is approximate.
- **Silence inventions:** without Silero voice detection on the Lemonade server, silence comes back as "Thank you." lines (see the README, "Stop invented text in silence").
- **Khmer:** Lemonade's Whisper can't do Khmer (it turns it into invented English). Khmer calls need `--language km` or `--spk-language km`; the text is real Khmer but with errors, and English words inside Khmer come out in Khmer letters.
- **Speaker labels:** quick back-and-forth with no pause can land two people in one line, and one person can get two letters if their voice changes a lot (distance from a room mic, volume).

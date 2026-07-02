# Cut Video

A [Claude Code](https://claude.com/claude-code) skill that tightens long-form recordings — removes silences, ums, fillers, and dead air **while preserving laughs and comedic pauses**.

**More accurate than cutting from Whisper alone.** Cuts are driven by [Montreal Forced Aligner](https://montreal-forced-aligner.readthedocs.io/) (MFA) word boundaries (~10–20ms precision, with true inter-word silences as explicit intervals) rather than Whisper timestamps (±100–300ms, with pauses embedded inside word durations). Whisper is used only to produce the transcript *text*; MFA aligns it to the audio. The result: tighter cuts, no clipped word onsets/tails, and reliable silence detection.

Point it at any video file and it will:

1. **Make a proxy** — transcodes HEVC/4K → 1080p H.264 with hardware decode so every subsequent step runs in seconds, not minutes (portrait 9:16 sources handled too).
2. **Transcribe with [Whisper](https://github.com/openai/whisper)** — `tiny.en`, for the transcript text only.
3. **Force-align with [MFA](https://montreal-forced-aligner.readthedocs.io/)** — snaps the transcript to the audio at ~10–20ms precision; this drafts the take structure and the cut list.
4. **Plan the cuts** — applies a tone-aware silence heuristic (aggressive / balanced / sentimental / documentary), strips fillers, drops false starts and retakes. Shows you the plan before touching the video.
5. **Ground-truth every keep boundary** — re-transcribes each kept region in an isolated window before rendering. On retake-heavy footage Whisper collapses repeated lines and MFA's global alignment drifts by *seconds*; isolated windows catch that, plus dropped words and flubs hiding inside stretched words. Pauses >0.2s *inside* kept takes get trimmed to ~0.1s here too.
6. **Render & verify** — ffmpeg `trim+concat` (not `select`, which misaligns audio), then re-transcribes the output and scans for surviving stutters or clipped words before shipping.

There's also an optional **browser timeline editor** (`make_review.py`) — a self-contained HTML page with a canvas waveform, pinch-to-zoom, drag-across-to-delete, auto-suggested silence bands, per-segment transcript cards, and a live "preview final cut" mode. It exports an edited keep list you paste back into the chat for a seconds-fast re-render.

Runs entirely on your machine. No cloud APIs. Built for Apple Silicon — fast.

## Why this exists

Most silence-cutters (auto-editor, opus, etc.) are either too aggressive (they cut your laughs) or too slow (the wrong ffmpeg flags). This skill was tuned against a 7-minute 4K HEVC interview that took 5+ minutes to even decode in other tools. With the right flags (`-hwaccel videotoolbox`, `-preset fast`, hardware-friendly trim/concat) the same source cuts in ~30 seconds.

Specifically, this skill is opinionated about four things most tools get wrong:

- **Laughs aren't silences.** Before any silence cut, it checks audio amplitude. If a "silent" window has audible content, it stays.
- **Comedic pauses are content.** A 4-second pause after a punchline isn't dead air — it's the joke. The skill compresses dead air aggressively but preserves comedic timing.
- **Audio sync matters.** Most `ffmpeg -vf select` cut tutorials produce videos with drifting audio. This skill uses `trim+concat`, which keeps everything in sync.
- **ASR timestamps lie on real footage.** Whisper collapses retakes, drops words ("bland", "slop"), and hides pauses inside stretched word durations — and a forced aligner fed that transcript drifts by seconds. The skill treats all global timestamps as drafts and ground-truths every cut boundary with isolated-window re-transcription, then verifies the rendered output.

## Requirements

- macOS (uses VideoToolbox for hardware-accelerated decode — works on Linux/Windows if you remove `-hwaccel videotoolbox` flags)
- [Claude Code](https://claude.com/claude-code)
- `ffmpeg` with `libx264` (`brew install ffmpeg`)
- [`whisper`](https://github.com/openai/whisper) (`pip install openai-whisper`) — for the transcript text
- [Montreal Forced Aligner](https://montreal-forced-aligner.readthedocs.io/) — the alignment / timing engine. **The skill installs this for you** on first run (creates a `mfa` conda env and downloads the English model; installs [miniforge](https://github.com/conda-forge/miniforge) via Homebrew if conda is missing). No manual setup needed. If Homebrew isn't available, the skill falls back to Whisper word timestamps + ffmpeg `silencedetect` (and says so in the plan).

## Install

```bash
git clone https://github.com/louisedesadeleer/cut-video.git ~/.claude/skills/cut-video
```

Restart Claude Code and `/cut-video` is available as a slash command.

## Usage

In Claude Code:

```
/cut-video
```

Then paste a video file path when asked. The skill will:

1. Make a 1080p H.264 proxy (skip if source is already small/H.264)
2. Transcribe the audio with Whisper (transcript text only)
3. Force-align the transcript to the audio with MFA — the draft timing for every cut
4. Print a proposed cut list — timestamps, what gets cut, what stays, total compression
5. Ground-truth the keep boundaries with isolated-window re-transcription
6. Wait for your "go" (ask for the timeline editor here if you want to adjust by hand)
7. Render the cleaned version, verify its transcript is clean, and open it for review

## Tone presets

When asked, pick one:

- **`aggressive`** *(default)* — tightest pacing; ~40–60% runtime reduction, mid-sentence cuts, fillers and hedges sliced
- **`balanced`** — aggressive on dead air, preserves comedic beats
- **`sentimental`** — gentler everywhere; longer pauses preserved for emotional moments
- **`documentary`** — only cuts genuine dead air; keeps thinking pauses intact

## What this skill doesn't do

- It doesn't add zooms, layouts, b-roll, memes, captions, or motion graphics. Pair it with [clipify](https://github.com/louisedesadeleer/clipify) for short-form output, or your editor of choice for the rest.

Built by [@louisedesadeleer](https://github.com/louisedesadeleer). PRs welcome.

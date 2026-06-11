---
name: cut-video
description: Tighten a long recording — remove silences, fillers, mistakes, and dead air while preserving laughs and comedic pauses. Use when the user pastes a video and says "cut this", "tighten this video", "remove silences", "strip ums", "clean this up", or any variant of "make this video shorter without losing the good parts". Fast (proxy + hardware decode + trim/concat) — runs in seconds, not minutes, on M-series Macs.
---

# Cut Video

Tighten a long-form recording **aggressively**: remove silences, fillers, hedges, weak transitions, false starts, and repetitions. Preserves laughs and comedic pauses. Outputs a cleaned MP4 ready to drop into CapCut for layouts/zooms/memes.

> **This skill is more accurate than cutting from Whisper alone.** Cuts are driven by **Montreal Forced Aligner (MFA)** word boundaries (~10–20ms precision, with true inter-word silences as explicit intervals), not Whisper timestamps (±100–300ms, with pauses embedded inside word durations). Whisper is used only to produce the transcript *text* that MFA aligns to the audio. The result: tighter cuts, no clipped word onsets/tails, and reliable silence detection.

**Style target** (calibrated from `AI mogging my dad.mp4`):
- ~1 cut every 1.3–1.5 seconds (median cut duration ~1.1s)
- ~40–60% total runtime reduction from raw
- Cuts happen mid-sentence, not just between sentences
- Word-by-word tightening — fillers, hedges, and weak openers get sliced

## Inputs

- Video file path (the user will provide; otherwise ask)
- Optional: tone preference (`aggressive` / `balanced` / `sentimental` / `documentary`) — default: `aggressive`

## Working directory

`/tmp/cut-video/<basename>/` — mkdir at start, leave artifacts for debugging.

---

## Step 0 — Make a proxy (the single biggest speed move)

If the source is HEVC, >500MB, or 4K+, transcode to a 1080p H.264 working copy FIRST. Every subsequent step runs against the proxy, not the source.

```bash
ffmpeg -y -hwaccel videotoolbox -i "$SRC" \
  -vf scale=1920:1080 \
  -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
  -c:a aac -b:a 192k \
  /tmp/cut-video/$NAME/proxy.mp4
```

**Critical flags:**
- `-hwaccel videotoolbox` on the INPUT — hardware-decodes HEVC, ~5–10× faster on Apple Silicon. Skip this and you'll wait minutes instead of seconds.
- `-preset fast` — the libx264 default is `medium`, which is ~3× slower for no useful quality gain on a working copy.

Hardware encode alternative (even faster on M-series, slightly larger file):
```bash
-c:v h264_videotoolbox -b:v 8M
```

## Step 1 — Get the transcript TEXT (whisper, as input for MFA)

MFA is a **forced aligner, not a transcriber** — it needs a transcript to align. Whisper's only job here is to produce that text; **whisper word timestamps are NOT used for cutting** (they're ±100–300ms off and turbo embeds pauses inside word durations). MFA (Step 1.5) supplies all timing.

```bash
ffmpeg -y -hwaccel videotoolbox -i /tmp/cut-video/$NAME/proxy.mp4 \
  -vn -ac 1 -ar 16000 /tmp/cut-video/$NAME/audio.wav
whisper /tmp/cut-video/$NAME/audio.wav \
  --model tiny.en --word_timestamps True --output_format json \
  --output_dir /tmp/cut-video/$NAME --language en
```

Why `tiny.en`: ~10× faster than `small.en`, and since we only need the words (not the timings), accuracy is plenty for English. For non-English use `--model base` and drop `--language`. We keep `--word_timestamps True` only so whisper times survive as a cross-check/fallback (Step 1.5 caveats, 2a-legacy) — never as the primary cut source.

## Step 1.5 — Align the transcript to the audio with MFA (Montreal Forced Aligner) — the timing engine

This is the spine of the skill. **MFA forced-aligns the whisper transcript text to the audio** and returns word boundaries at ~10–20ms precision, with true inter-word silences as explicit empty intervals. **All cut decisions (gaps, fillers, false starts, retakes) use MFA word times.**

**One-time setup (conda is the only supported install path):**
```bash
conda create -n mfa -c conda-forge montreal-forced-aligner -y
conda run -n mfa mfa model download acoustic english_mfa
conda run -n mfa mfa model download dictionary english_mfa
```

**Per run:**
```bash
# corpus = a dir with audio.wav + audio.txt (the whisper transcript as PLAIN TEXT, no timestamps)
mkdir -p /tmp/cut-video/$NAME/corpus
cp /tmp/cut-video/$NAME/audio.wav /tmp/cut-video/$NAME/corpus/
python3 -c "import json;d=json.load(open('/tmp/cut-video/$NAME/audio.json'));open('/tmp/cut-video/$NAME/corpus/audio.txt','w').write(d['text'])"
conda run -n mfa mfa align --clean /tmp/cut-video/$NAME/corpus english_mfa english_mfa /tmp/cut-video/$NAME/aligned
# → aligned/audio.TextGrid — "words" tier: (start, end, word); EMPTY-label intervals = true silences
```
Parse the TextGrid `words` tier (pip `praatio`, or a 20-line parser — interval tiers are plain text). What MFA buys:
- **True word boundaries** → cut pads shrink to ~0.02s, no clipped word onsets/tails.
- **True silences are explicit empty intervals** → the 2a gap table works directly off the TextGrid; the whisper embedded-pause problem disappears.
- **Caveat:** MFA aligns the transcript it's GIVEN. If whisper collapsed a spoken stutter ("growth… growth marketer" → "growth marketer"), alignment drifts locally around it — keep the `silencedetect` pass (2a) as a CROSS-CHECK, and treat disagreements > 0.3s as suspect regions to re-inspect.
- **Fallback:** if MFA/conda is unavailable and the user doesn't want the install, fall back to whisper words + audio silencedetect (2a) — and say so in the plan summary. This is the ONLY path where whisper timestamps drive cuts.

## Step 2 — Build the cut list (AGGRESSIVE by default)

Compute a list of (start, end) intervals to KEEP from the **MFA word intervals** (whisper JSON only as fallback). Aggressive cutting removes content in four categories:

### 2a. Silence gaps — MFA empty intervals first, audio silencedetect as cross-check (updated 2026-06-10)
With MFA (Step 1.5), true silences are the TextGrid's empty-label intervals — apply the gap table below to those directly. Cross-check with audio silence detection:
```bash
ffmpeg -i proxy.mp4 -af silencedetect=noise=-30dB:d=0.14 -f null - 2>&1 | grep silence_
```
Without MFA, the silencedetect pass is the ONLY trustworthy gap source: **`mlx_whisper`/whisper-turbo EMBEDS pauses inside word durations** — a 3s "word" is really "word [long pause]", inter-word *gaps* read ~0.00, and gap-based trimming does NOTHING. Parse `silence_start`/`silence_end`, remove those intervals (keep ~0.04s pad; MFA-precision boundaries allow ~0.02s). For "almost no pauses" → `d=0.12`, pad 0.03. Combine with the dedup (2c/2d) by removing `silences ∪ dropped-word-ranges`. This is how you hit median ~1.1–1.3s.
- **Stutters whisper hides:** turbo collapses spoken repetitions ("growth… growth marketer" → "growth marketer") and varies run-to-run (drops/re-adds words). These are invisible to transcript-based cutting — MFA alignment drift + silencedetect disagreement marks the region; re-inspect it, or ask the user for the timecode and cut a precise window.

### 2a-legacy. Silence gaps between words (TIGHT thresholds)

| Gap | `aggressive` (default) | `balanced` | `sentimental` | `documentary` |
|---|---|---|---|---|
| `< 0.25s` | keep | keep | keep | keep |
| `0.25–1s` | trim to **0.1s** | trim to 0.3s | trim to 0.5s | keep |
| `1–3s` | trim to **0.2s** | trim to 0.8s | trim to 1.5s | trim to 2s |
| `> 3s` | trim to **0.2s** | trim to 0.5s | trim to 1s | trim to 3s |

The aggressive thresholds match the reference video's pacing (median cut 1.13s).

### 2b. Filler words — cut with 50ms padding

Always cut: `um`, `uh`, `umm`, `uhh`, `er`, `erm`, `ah`, `mhm`, `hmm` (standalone)

### 2c. Hedges & weak transitions — cut aggressively in default mode

Cut these when they're delivered as filler (not as substantive content). Mark for cut, then verify against context in the review step:

- **Hedges:** `like` (as filler, not as comparison), `you know`, `I mean`, `I guess`, `I think` (when hedging, not asserting), `kind of`, `sort of`, `basically`, `literally`, `actually` (as filler)
- **Weak transitions:** `so` (sentence-opener), `and so`, `and then`, `but um`, `but uh`, `okay so`, `right so`, `well`
- **Redundant qualifiers:** `kind of like`, `sort of like`, `or whatever`, `or something`
- **Re-orientation phrases:** `anyway`, `so anyway`, `the thing is`, `what I'm saying is`, `to be honest`, `honestly`

In `balanced` / `sentimental` / `documentary` modes, only cut hedges that precede a re-statement (false start, see 2d). Keep them in conversational moments.

### 2d. False starts and repetitions — cut aggressively

Detect by scanning for:
- **Self-corrections:** speaker re-states the same opening within ~3 seconds. Pattern: `"I want — I wanted to..."`, `"It's a — it's an interesting..."`. Drop the earlier attempt + the gap.
- **Restart preambles:** `"so basically what I'm saying is..."`, `"the point is..."`, `"let me start over..."`. Drop the preamble, keep the actual point.
- **Verbatim repetitions:** speaker says same 3+ words twice in a row. Keep the cleaner take.

Implementation hint: build a sliding-window fuzzy-match on consecutive word n-grams. When 3-gram similarity > 0.8 within a 3-second window, flag the earlier instance for removal.

### 2d-bis. FULL-LINE RETAKES (self-shot to-camera recordings, added 2026-06-10)
Self-shot intros/promos contain **whole-sentence retakes** separated by 5–60s ("The next person is Carmen who works. … The next person, the next guest I invited is Carmen who's social and content lead at Slate") — far outside 2d's 3-second window. Detect and resolve them:
- **Detection:** fuzzy n-gram match (4-grams, similarity > 0.7) across a **60-second window**; also match sentence OPENERS (first 3–5 words) since retakes usually restart the line.
- **Keep the LAST take by default** — speakers re-record until satisfied. Exception: if the last take is incomplete/flubbed and an earlier one is clean, keep the clean one and note the override in the plan summary.
- **A retake boundary is usually preceded by a long pause, a breath, a click, or a slate-phrase** ("okay", "again", "take two") — the pause+opener-match combo is the strongest signal.
- **Surface every retake group in the Step 3 plan** (`line → takes at [t1, t2, t3] → keeping t3`) so the user can override which take survives.

### 2e. NEVER cut these — protect explicitly

- **Laughs.** Whisper transcribes laughter as silence. Before applying ANY silence cut, check audio amplitude:
  ```bash
  ffmpeg -i audio.wav -af "volumedetect" -f null - 2>&1 | grep mean_volume
  ```
  If a flagged "silence" window has peaks > -30dB or RMS > -25dB, it contains audible content — keep it. Use `ffmpeg -i audio.wav -af "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level"` for per-window RMS if needed.
- **Comedic beats.** A deliberate pause after a punchline. In `aggressive` mode, still trim to 0.4–0.6s (don't kill it, just tighten).
- **Reaction sounds.** Sighs, "ooh", "wow", gasps — all content.
- **Single-word punchlines.** If a sentence ends with a one-word zinger after a beat, keep the beat.

### 2f. STRICTNESS PASS — zero tolerance for repetitions (Louise, 2026-06-10: "get rid of ANY repetitions")
After building the keep list, run a machine scan — do NOT trust your eyes on the transcript:
1. **Scan the MFA words that fall INSIDE kept ranges** for (a) consecutive duplicate words, (b) duplicate bigrams ACROSS segment boundaries (a flubbed restart can straddle a cut — "…meet her. And we're [flub] / And we're going to…" survived a snap once), (c) duplicate sentence-openers in adjacent sentences ("So… So…" → cut the second).
2. **Cut hedge flubs** even mid-stat: "I believe", opener "Like", "I guess". Keep rhetorical repetition (parallel structure) — that's intentional.
3. **When excising a word, cut to the FULL MFA word end (+0.01s)** — a 15ms vowel residue still reads as the word.
4. **MFA OOV trap (burned 2026-06-10): acronyms/names missing from MFA's dictionary ("AI", brand names) silently VANISH from the alignment** — the word gets absorbed into its neighbor, and snapping a keep-end to "the last MFA word" then CLIPS the real spoken word. Guard: if whisper's chunk end exceeds the last MFA word end by >0.2s, trust the LATER of (whisper end, next silencedetect onset) for that boundary — and verify that junction with an isolated-segment transcription.
5. **Verification trap: whisper-of-the-output LIES twice.** It (a) COLLAPSES surviving stutters (a real "and we're… and we're" transcribes once — looks clean, isn't), and (b) HALLUCINATES context words at cut junctions (a "girls"+"Enjoy" junction transcribes as "So enjoy" — looks dirty, isn't). So verify with BOTH: the kept-range MFA dup-scan (catches real stutters), and per-segment isolated transcription of any suspect junction (clears hallucinations). Only ship when the dup-scan returns NONE.
6. **⭐ CROSS-ASR VERIFICATION — the strongest stutter detector (discovered 2026-06-10).** The MFA dup-scan only sees the words WHISPER transcribed — if whisper collapsed a phrase retake ("with the most recent video… with the most recent video" → once), MFA never knows it exists and the scan passes a dirty cut. The fix: **run the cut through a SECOND, independent ASR and diff.** Tella's transcript after upload is perfect for this (different model → different blind spots): it caught 5 repetitions in one pass that the whisper+MFA pipeline certified clean (a doubled phrase, a surviving "or, or", two mid-word fragments "pers-"/"per-", a doubled "into"). Workflow: upload the cut to Tella (or any second ASR) → scan ITS transcript for dup words/bigrams/phrases and mid-word fragments ("xyz-") → map hits back to source times → cut → re-verify. Disagreement between the two ASRs marks exactly the regions to fix or flag to the user.

## Step 3 — Show the plan, get confirmation

Print a summary BEFORE rendering:

```
Original duration: 7m 25s
Cleaned duration: 3m 58s
Cut: 3m 27s (47%)
Total keep intervals: 312
Median cut duration: 1.2s (target: 1.1–1.5s)
Hard cuts (dead air > 3s removed): 23
Filler hits: 47
Hedge/transition cuts: 89
False-start drops: 12
Notable preserved long pauses: list timestamps + context (laughs, punchlines)
```

**Self-check vs benchmark:**
- If median cut > 2s → not aggressive enough. Tighten gap thresholds or expand hedge list, retry.
- If median cut < 0.7s → over-cut. Likely killing breath/cadence. Loosen 0.25–1s threshold to 0.3s.
- Target compression: 40–60% in aggressive mode.

Wait for "go" before rendering. Don't render speculatively.

## Step 4 — Render with `trim` + `concat`, NOT `select`

**Critical:** Use the trim+concat filtergraph pattern, NOT `select`/`aselect`. The select filter compresses video frames but does NOT properly compress audio PTS, leaving audio and video misaligned.

Build a filtergraph with one trim+atrim pair per keep interval, then concat them:

```
[0:v]trim=A1:B1,setpts=PTS-STARTPTS[v0];
[0:a]atrim=A1:B1,asetpts=PTS-STARTPTS[a0];
[0:v]trim=A2:B2,setpts=PTS-STARTPTS[v1];
[0:a]atrim=A2:B2,asetpts=PTS-STARTPTS[a1];
...
[v0][a0][v1][a1]...concat=n=N:v=1:a=1[outv][outa]
```

Write the filtergraph to a file (it'll get long — aggressive cuts mean 200–400 intervals on a 7-min source) and use `-filter_complex_script`. Render call:

```bash
ffmpeg -y -hwaccel videotoolbox -i /tmp/cut-video/$NAME/proxy.mp4 \
  -filter_complex_script /tmp/cut-video/$NAME/filter.txt \
  -map "[outv]" -map "[outa]" \
  -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
  -c:a aac -b:a 192k \
  /tmp/cut-video/$NAME/cleaned.mp4
```

**Faster alternative for many cuts:** if filtergraph exceeds ~500 trims, switch to per-segment extract + lossless concat:
1. Extract each keep interval as a separate stream-copied chunk
2. Build a concat demuxer file
3. `ffmpeg -f concat -safe 0 -i concat.txt -c copy cleaned.mp4`

This avoids re-encoding entirely on the trim pass.

## Step 5 — Deliver

- Save final to `<source_dir>/cut_out/<source_name>_cut.mp4` (mkdir if missing)
- Print one line: original duration → cleaned duration, percent cut, median cut, output path
- `open` the file so the user can review immediately
- Offer to iterate: re-tune gap thresholds, switch tone preset, mark specific moments to preserve/cut

---

## Pitfalls — don't repeat these (learned from prior runs)

- **Don't skip `-hwaccel videotoolbox`** on the proxy step. HEVC software decode on a 7-min 4K source can take 5+ minutes. With the flag, ~30s.
- **Don't use the `select` filter for cuts.** It misaligns audio. Use trim+concat.
- **Don't use `-preset medium` (the libx264 default).** It's ~3× slower than `-preset fast` for no quality gain on a working copy.
- **Don't render before the user confirms the cut list.** Sometimes "silent" gaps contain laughs the user wants to keep; sometimes a "filler" is intentional emphasis. Print, wait, then render.
- **Don't trim a long "silence" without checking amplitude** — that's usually laughter, a thinking pause, or a setup-payoff beat.
- **Don't `whisper` the entire raw source if a proxy exists.** Run whisper against `audio.wav` extracted from the proxy.
- **Don't re-encode audio twice.** If you only changed video, use `-c:a copy` to skip an unnecessary AAC pass.
- **Don't be timid.** Default to `aggressive`. The reference cut style is fast — if the output feels "safe", it's not matching Louise's CapCut pacing.

## Benchmark reference

`AI mogging my dad.mp4` (2026-05-03, CapCut project `0501`):
- 335 cuts in 460s = 1 cut / 1.37s
- Median 1.13s, mean 1.37s
- 41% of cuts < 1s, 12% < 0.5s
- Only 3 cuts > 5s
- Raw source compressed ~60%

Use this as ground-truth for "aggressive" mode tuning.

## What this skill explicitly does NOT do

- Add zooms, layouts, or motion graphics (separate concern — handled in CapCut or via [[tella-edit]])
- Add memes or b-roll (user-curated, see [[clipify]] for short-form cuts)
- Burn captions (separate pass after cleanup)
- Upload anywhere

Keep this skill focused on one thing: produce a tighter MP4 from a long-form recording, fast and aggressive.

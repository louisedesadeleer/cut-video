---
name: cut-video
description: Tighten a long recording — remove silences, fillers, mistakes, and dead air while preserving laughs and comedic pauses. Use when the user pastes a video and says "cut this", "tighten this video", "remove silences", "strip ums", "clean this up", or any variant of "make this video shorter without losing the good parts". Fast (proxy + hardware decode + trim/concat) — runs in seconds, not minutes, on M-series Macs.
---

# Cut Video

Tighten a long-form recording: remove silences, fillers, mistakes, and dead air. Preserves laughs and comedic pauses. Outputs a cleaned MP4.

## Inputs

- Video file path (the user will provide; otherwise ask)
- Optional: tone preference (`playful` / `sentimental` / `documentary`) — affects how aggressively to trim comedic pauses. Default: `playful`.
- Optional: strictness (`strict` / `default` / `loose`) — affects how aggressively to drop retakes, false starts, and mistake-and-restart sentences (see Step 2.5). Default: `default`.

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

- `-hwaccel videotoolbox` on the INPUT — hardware-decodes HEVC, ~5–10× faster on Apple Silicon. Skip this and you'll wait minutes instead of seconds on a 4K HEVC source.
- `-preset fast` — the libx264 default is `medium`, which is ~3× slower for no useful quality gain on a working copy.

Hardware encode alternative (even faster on M-series, slightly larger file):

```bash
-c:v h264_videotoolbox -b:v 8M
```

## Step 1 — Transcribe with whisper

```bash
ffmpeg -y -hwaccel videotoolbox -i /tmp/cut-video/$NAME/proxy.mp4 \
  -vn -ac 1 -ar 16000 /tmp/cut-video/$NAME/audio.wav
whisper /tmp/cut-video/$NAME/audio.wav \
  --model tiny.en --word_timestamps True --output_format json \
  --output_dir /tmp/cut-video/$NAME --language en
```

Why `tiny.en`: ~10× faster than `small.en`, quality fine for English. For non-English use `--model base` and drop `--language`.

## Step 2 — Build the cut list

Parse the whisper JSON's word timestamps and compute a list of (start, end) intervals to KEEP. Heuristic for gaps between consecutive words:

| Gap | Action |
|---|---|
| `< 0.6s` | Keep as-is (natural breath) |
| `0.6–2.5s` | Trim to 0.4s (snappy) |
| `2.5–5s` | Trim to 1.5s (preserve comedic beat) |
| `> 5s` | Trim to 0.5s (dead air, loading time) |

**Also cut:**

- Filler words: `um`, `uh`, `umm`, `uhh`, `er`, `erm` (with 50ms padding around each)
- False starts, retakes, and mistake-and-restart sentences — see **Step 2.5** for the explicit detection rules. Don't try to handle these in this step's gap heuristic; they need dedicated transcript analysis.

**NEVER cut laughs.** Whisper sometimes emits long silent gaps where there's actually audible laughter. Before applying any silence cut, check audio amplitude:

```bash
ffmpeg -i audio.wav -af "volumedetect" -f null - 2>&1 | grep mean_volume
```

If a flagged "silence" window has peaks > -30dB, it contains content — keep it. Lean toward keeping any 1s+ gap if there's any audible signal.

**Tone-aware adjustment:**

- `playful` (default): apply the heuristic above
- `sentimental`: bump all preserved-pause targets up (0.6–2.5 → 0.8s, 2.5–5 → 2.5s, >5 → 1.0s). Don't rush emotional moments.
- `documentary`: even more conservative; only cut > 8s dead air

## Step 2.5 — Detect retakes and mistakes

Raw recordings — especially first-take screen recordings — are full of full-sentence retakes ("Now that Claude can edit your videos..." attempted 3 times before the clean take) and mid-sentence corrections ("So I wanted to make a way. So I. So I created..."). Whisper transcribes all of them; the gap heuristic in Step 2 won't catch them because they have content and the gaps between them are short. Handle these explicitly here, after the gap pass but before rendering.

### 2.5a — Mistake-and-restart sentences

Scan the transcript for sentence boundaries that end in a **retract marker** within the last ~3 words. Retract markers:

```
wait,  /  sorry,  /  actually,  /  let me,  /  let me try that again  /
no —  /  scratch that  /  hold on  /  okay so —  /  one sec
```

When you hit one: drop everything from the start of the marker's containing sentence back to the most recent sentence boundary (period, question mark, or > 0.8s gap). The next sentence is the clean take.

### 2.5b — Retake detection via n-gram similarity

For every sentence-initial 4-gram in the transcript (the first 4 content words of each sentence after stopword stripping), check whether the same or near-identical 4-gram appears again within the next ~45 seconds.

A near-identical match = Jaccard similarity ≥ 0.75 OR ≥ 3 of the 4 content words match (case-insensitive, lemmatized). Stopwords to strip before comparison: `the, a, an, and, or, but, so, that, this, is, are, was, were, i, you, it, to, of, in, on, for`.

When you find a repeated opening:

1. The **later** occurrence is the keeper (people self-correct toward the clean take).
2. Drop from the **earlier** occurrence's start through the moment just before the later occurrence begins (snap to the nearest sentence boundary or > 0.4s gap on each end).
3. If there are 3+ matching openings (common with "Now that Claude can edit your videos..."), drop everything except the **last** one.

Edge case: if two matching openings are spoken close together with no diverging content between them (< 1.5s gap, < 5 words between), treat the second as a stammer-and-restart rather than a full retake — drop only the first repetition, not everything between them.

### 2.5c — Strictness scaling

Apply the strictness input to the thresholds above:

| Setting | Retake search window | Similarity threshold | Mistake-marker action |
|---|---|---|---|
| `strict` | 90s | Jaccard ≥ 0.60 OR 2/4 words | Also drop sentences containing inline `i mean,` / `or rather,` |
| `default` | 45s | Jaccard ≥ 0.75 OR 3/4 words | As above |
| `loose` | 20s | Jaccard ≥ 0.90 OR 4/4 words | Skip mistake-marker rule entirely |

Default is reasonable for most recordings. Use `strict` for unscripted explainers and demos (lots of self-correction). Use `loose` for already-tight recordings where you just want filler and silence cleanup.

### 2.5d — Surface the cuts

Add to the Step 3 plan summary:

```
Retakes dropped:        N  (earliest of M attempts kept the final one)
Mistake-restarts dropped: N  (sentences ending in 'wait,' / 'sorry,' / 'actually,' / ...)
```

For each retake, print the matched opening phrase and the timestamps of all attempts so the user can sanity-check before rendering. **Never cut a retake silently** — when the wrong take is kept it's the most visible failure mode of this skill.

## Step 3 — Show the plan, get confirmation

Print a summary BEFORE rendering:

```
Original duration: 7m 25s
Cleaned duration: 5m 58s
Cut: 1m 27s (20%)
Hard cuts (dead air > 5s): N
Filler hits: N
Notable preserved long pauses: list timestamps + context
```

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

Write the filtergraph to a file (it'll get long) and use `-filter_complex_script`. Render call:

```bash
ffmpeg -y -hwaccel videotoolbox -i /tmp/cut-video/$NAME/proxy.mp4 \
  -filter_complex_script /tmp/cut-video/$NAME/filter.txt \
  -map "[outv]" -map "[outa]" \
  -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p \
  -c:a aac -b:a 192k \
  /tmp/cut-video/$NAME/cleaned.mp4
```

## Step 5 — Deliver

- Save final to `<source_dir>/cut_out/<source_name>_cut.mp4` (mkdir if missing)
- Print one line: original duration → cleaned duration, percent cut, output path
- `open` the file so the user can review immediately
- Offer to iterate: re-tune gap thresholds, switch tone preset, mark specific moments to preserve/cut

---

## Pitfalls — don't repeat these (learned from prior runs)

- **Don't skip `-hwaccel videotoolbox`** on the proxy step. HEVC software decode on a 7-min 4K source can take 5+ minutes. With the flag, ~30s.
- **Don't use the `select` filter for cuts.** It misaligns audio. Use trim+concat.
- **Don't use `-preset medium` (the libx264 default).** It's ~3× slower than `-preset fast` for no quality gain on a working copy.
- **Don't render before the user confirms the cut list.** Sometimes "silent" gaps contain laughs the user wants to keep; sometimes a "filler" is intentional emphasis. Print, wait, then render.
- **Don't trim a 10s+ "silence" without checking amplitude** — that's usually laughter, a thinking pause, or a setup-payoff beat.
- **Don't whisper the entire raw source if a proxy exists.** Run whisper against `audio.wav` extracted from the proxy.
- **Don't re-encode audio twice.** If you only changed video, use `-c:a copy` to skip an unnecessary AAC pass.
- **Don't rely on Step 2's gap heuristic to catch retakes.** Full-sentence retakes have content and small gaps — they slip right through. Run Step 2.5 explicitly.
- **Don't silently drop a retake.** Print the matched opening phrase and timestamps so the user can spot a wrong-take-kept before rendering.

## What this skill explicitly does NOT do

- Add zooms, layouts, or motion graphics (separate concern)
- Add memes or b-roll (user-curated)
- Burn captions (separate pass after cleanup)
- Upload anywhere

Keep this skill focused on one thing: produce a tighter MP4 from a long-form recording, fast.

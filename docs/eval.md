# Detection quality

`PLAN.md` §12's quality loop. Every tuning decision in this project — which model, how much
padding, where the guards sit — was previously argued from a handful of observations. This is
where those arguments get settled with numbers.

> **Status: 9 of 15 labels verified by ear.** Timing error is measured over those 9. The other
> 6 are clip c2, which cannot be labelled by hand — see *Ground truth*. Precision needs
> `--repeat`; see *Run-to-run stability*.

## Running it

```bash
cd backend && uv run --extra stt python -m scripts.eval run --media-dir ../video --models base,large-v3-turbo --modes windowed,full --out ../docs/eval.md
```

`validate` parses the labels without needing any media. `run` skips cleanly, exit 0, when the
media is absent — it is a developer tool, not CI.

## Ground truth, and what it can support

The labelled media is one episode, **PLURIBUS S01E01**. It is not in this repository and is not
ours to distribute; the labels reference it by name and are useless without it. That is the
intended trade — the timings are the valuable part and they are tiny.

**Presence is solid.** The English subtitles name the words that are spoken, so "is there a
`fuck` in this clip" is knowable without hearing it. Precision and recall mean what they say.

**Timing needs a human, and is tracked per label.** A word boundary has to come from someone
hearing it; a machine-seeded boundary is only the opinion of whichever model seeded it, and
scoring a model against its own output is circular. Each label therefore carries a `verified`
flag, and timing error and mute coverage are measured over **only the verified ones**, with the
count in the `n` column. Presence still counts every label.

Per label rather than all-or-nothing, because **some clips cannot be labelled by hand at all**.
Clip c2 is six shouted repetitions of the same word running together; the words are certainly
there, so it remains good ground truth for presence, but nobody can place their boundaries on a
waveform. Requiring a fully verified set before reporting any timing would have meant reporting
timing never.

**One further bias worth stating.** The labels were seeded from a *windowed* `large-v3-turbo`
run, so they under-count words that only a full pass hears. Some of the false positives below
are very likely real profanity missing from the label set rather than detector errors.

## The clips

Five clips, chosen for distinct failure modes rather than density.

| id | span | why |
|---|---|---|
| c1 | 1765–1790 | three subtitle-only fallbacks — STT located none of them, so each mutes ~1.2 s where the word is ~0.3 s |
| c2 | 1830–1846 | a dense run of `fuck`, subtitle and STT agreeing, plus one STT-only hit at 0.22 confidence. **Presence only** — the repetitions run together and cannot be separated by hand |
| c3 | 3015–3055 | `goddamn` immediately followed by a bare `god` — the compound-rollup case |
| c4 | 1850–1870 | a subtitle-only `god` beside located `fuck`s — religious against strong |
| c5 | 600–640 | **control, no labels.** Any detection here is a false positive |

The control clip earns its place: precision computed only over labelled regions cannot catch a
detector that fires everywhere.

## Metrics

* **TP/FP/FN** — a detection matches a label when the canonical agrees and the spans are within
  0.5 s. Matching is greedy nearest-midpoint and one-to-one, so two detections cannot both claim
  one label and inflate recall.
* **median / mean timing error** — §12 asks for the mean; both are reported. M1 already saw a
  single 4.1 s whisperX span, and one outlier like that makes a mean describe the outlier rather
  than the model.
* **n** — how many labels were verified by ear, and therefore how many the timing columns and
  mute coverage are computed over. Presence uses all of them.
* **FP range** — with `--repeat`, the lowest and highest false-positive count seen. See below.
* **mute coverage** — the fraction of each labelled word that the *padded, merged* mute ranges
  actually silence. This is the metric that corresponds to "did the viewer hear it". Detection
  precision and recall do not: a detection whose range lands 200 ms short still leaves the word
  audible. It is consistently the lowest number in the table, and it is the one to optimise.

<!-- BEGIN RESULTS -->

| Model | Mode | TP | FP | FN | P | R | F1 | median err | mean err | n | FP range | mute cov | wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | windowed | 13 | 6 | 2 | 0.68 | 0.87 | 0.76 | 0.425s | 0.367s | 9 | 4-7 | 0.85 | 284s |
| base | full | 7 | 9 | 8 | 0.44 | 0.47 | 0.45 | 0.111s | 0.179s | 6 | 4-75 | 0.50 | 227s |
| large-v3-turbo | windowed | 13 | 10 | 2 | 0.57 | 0.87 | 0.68 | 0.154s | 0.258s | 9 | 4-11 | 0.84 | 274s |
| large-v3-turbo | full | 12 | 3 | 3 | 0.80 | 0.80 | 0.80 | 0.134s | 0.148s | 7 | 1-3 | 0.68 | 279s |

<!-- END RESULTS -->

## Run-to-run stability — read this before comparing precision

faster-whisper on CPU with `int8` and multiple threads is **not deterministic**, and the
difference is not small. Three identical runs of `base` / windowed produced:

| run | TP | FP | FN | P |
|---|---|---|---|---|
| 1 | 13 | **8** | 2 | 0.62 |
| 2 | 14 | **12** | 1 | 0.54 |
| 3 | 13 | **72** | 2 | 0.15 |

The 72 is not a bug in the harness: it is `base` entering a **repetition loop** on clip c2's
shouting and emitting `fuck` dozens of times at the same timestamp. Whether it does so turns on
tiny numerical differences between runs, so it is bistable — it either happens or it does not.

Consequences, which shape how this table should be read:

* **Recall and timing error are stable.** Across those three runs TP moved 13/14/13 and the
  median timing error stayed within about 20 ms. Those numbers can be trusted from one run.
* **Precision is not.** A false positive is usually degeneration, so a single-run precision
  figure is close to meaningless for a weak model.
* `large-v3-turbo` is markedly steadier — 4 and 9 false positives across two runs, no
  degeneration — which is an argument for the shipped default beyond raw accuracy.

The `--repeat N` flag therefore runs each cell N times and reports the **median run**, chosen by
F1, with the observed false-positive range in the `FP range` column. The median *run* rather
than the median of each column separately, so every figure in a row comes from one real
execution instead of a combination that never happened. The table below uses `--repeat 3`.

## What these numbers changed

**Silero VAD is now off by default.** This is the headline result of building the harness.

faster-whisper documents that *"vad_filter will be ignored if clip_timestamps is used"*, and a
windowed pass always sets `clip_timestamps` — so VAD has **never** applied to this project's
default mode, despite §3 leaning on it to suppress hallucination. It applies only to a full
pass, and there it is actively harmful:

| full mode | P | R | F1 | mute cov |
|---|---|---|---|---|
| `vad_filter=True` | 0.67 | **0.11** | 0.19 | 0.11 |
| `vad_filter=False` | 0.69 | **0.61** | 0.65 | 0.50 |

(Measured against the earlier, unverified label set — before c1/c3/c4 were corrected by ear —
so these figures are not directly comparable with the table above. The conclusion does not
depend on them: it rests on Silero reporting zero seconds of speech in sixteen seconds of
dialogue, which is measurable directly.)

Measured directly on clip c2 — sixteen seconds of shouted dialogue — Silero reports **zero
seconds of speech** at its default threshold, and 0.9 s at a threshold of 0.2. No threshold
rescues it. VAD cost 5.5× the recall and bought no precision at all, on exactly the files full
mode exists to serve: the ones with no subtitles, where there is nothing else to fall back on.

**`large-v3-turbo` is the right default**, on both accuracy and stability. `base` is cheaper but
degenerates on hard audio (see above) and its recall in full mode is poor. `base` remains fine
for the drift probe, which only needs enough words to align against, and is not affected by the
occasional repetition loop because drift takes a median over many anchors.

**Windowed remains the default mode.** Full mode is competitive on quality but transcribes the
whole file; windowed touches about 5% of an episode's runtime. Full is for files with no usable
subtitles, which is exactly what M2 built it for.

### What the verified table says

* **`large-v3-turbo` in full mode is the strongest and steadiest cell** — F1 0.80, and a
  false-positive range of just 1–3 across three runs.
* **Timing separates the models clearly**: median error 0.134–0.154 s for `large-v3-turbo`
  against 0.425 s for `base` in windowed mode. That is the comparison the verified labels were
  needed for, and it could not be made at all before.
* **`base` in full mode is the worst of both worlds** — recall 0.47 and a false-positive range
  of 4 to 75. It should not be used for a full pass.
* **Precision is understated across the board.** The labels were seeded from a windowed
  `large-v3-turbo` run, so a detection of a word that genuinely is spoken but was never labelled
  counts as a false positive. `large-v3-turbo`/windowed scoring *lower* precision than `base`
  is very likely this effect rather than a real difference.

## The M2 demo: a 56-minute episode with its subtitles removed

§11 asks for "a movie with no subs processed overnight; timing error report". Run on
PLURIBUS S01E01 with every subtitle stream stripped, so the pipeline had nothing but audio.

It auto-promoted to a full pass (`mode=full`, `reason=no_subtitles`) and transcribed
**3,236 words in 560 segments** with `medium` — note the segment count, since the
pre-M2 alignment would have collapsed all 3,236 words into one segment. Transcription took
**19.6 minutes for 56.5 minutes of audio, about 2.9× realtime** on 14 threads, comfortably
faster than §3's 1.5–2× estimate.

| | windowed (M1, with subtitles) | full (M2, subtitles removed) |
|---|---|---|
| detections | 49 | **40** |
| muted | 36.1 s over 43 ranges | **26.3 s over 37 ranges** |
| flagged suspicious | 10 | **1** |
| words | fuck 19, god 12, shit 8, goddamn 3, bullshit 3, jesus 2, christ 1, god damn 1 | fuck 15, god 10, shit 7, bullshit 2, goddamn 2, jesus 2, christ 1, god damn 1 |

Full mode recovers **82% of the windowed detections with no subtitles at all**, and mutes
**10 seconds less** doing it — because the windowed run's ten subtitle-only fallbacks each
smear 1.2–1.9 s across a word that is nearer 0.3 s, while every full-mode range is located by
STT.

### Timing error report

Comparing the 27 detections both runs found:

| compared against | median | mean | max |
|---|---|---|---|
| all matched detections | **+0.009 s** | +0.083 s | 0.74 s |
| windowed `source=both` rows (subtitle + STT agreeing) | **+0.004 s** | +0.033 s | — |
| windowed `source=subtitle` fallbacks | **+0.551 s** | +0.551 s | — |

Where both modes locate a word by speech recognition they agree to within about 9 ms. Where the
windowed run had to fall back to a subtitle's proportional span, the two disagree by more than
half a second — and it is the *fallback* that is wrong. That is the quantitative version of a
claim M1 could only assert.

### What each mode found that the other missed

Full mode missed 22 windowed detections and found 13 the windowed run did not. The two dense
shouting sequences at 1777–1792 s and 1834–1843 s split cleanly: full mode caught the first,
windowed caught the second. This is the strongest argument for §6's audit pass — the union is
better than either alone.

Three of the 13 full-only detections carry a confidence below 0.01, which is almost certainly
recognition noise rather than speech. **A confidence floor for `source="stt"` detections is
worth considering**, and the harness can now measure whether one helps.

## Verifying the labels by ear

This is the one part of §12 that cannot be automated. Until it is done, timing error stays
withheld. Two routes; both rewrite the label file in place and set `verified: true`.

### Quick pass, no extra software

```bash
cd backend && uv run python -m scripts.eval verify --media-dir ../video
```

Plays each unverified label's exact span through `afplay` (or `ffplay`) and waits:

| key | |
|---|---|
| `enter` / `y` | boundaries are right — mark verified |
| `r` / `c` | replay the span / replay with 1.5 s of context |
| `a` / `A` | move the **start** 50 ms earlier / later |
| `z` / `Z` | move the **end** 50 ms earlier / later |
| `w WORD` | correct the word |
| `d` | drop the label — not actually profanity here |
| `s` / `q` | skip / save and quit |

It saves on quit, so it can be done in several sittings; `verify` only offers labels that are
still unverified. Listen for the word to be *fully* enclosed — a boundary 50 ms inside the word
is what leaves an audible fragment after muting.

### Precise pass, in Audacity

Boundaries are much easier to place by eye on a waveform than by ear, so for the final pass:

```bash
cd backend && uv run python -m scripts.eval export-audacity --media-dir ../video
```

That writes a `.wav` and a matching `.txt` per clip into **`eval-clips/<name>/`** at the repo
root. Deliberately not under `.local/`: these files get opened from Audacity's file dialog, and
a hidden directory is not reachable from a GUI file picker. It is gitignored, since it holds
audio cut from the media. `--dest` puts them elsewhere.

In Audacity: open `c1.wav`, then
**File > Import > Labels** for `c1.txt`; the labels appear as a track under the waveform and the
boundaries drag directly. When done, **File > Export > Export Labels** back over the same
`c1.txt`, then:

```bash
cd backend && uv run python -m scripts.eval import-audacity
```

The `.txt` is three tab-separated fields — start, end, text — in *clip* time; the importer
converts back to episode time. Any editor that reads that format works.

### When they are all verified

`validate` reports the count, and `run` starts printing real median and mean timing error
instead of `unverified`. That is the number that decides padding.

## Known gaps

* 9 of 15 labels are verified. The remaining 6 are clip c2, which cannot be labelled by hand;
  either accept it as presence-only, or replace it with a clip whose words are separable.
* Mute coverage is the number to optimise. Some of the shortfall is timing error and some is
  padding; a padding sweep (`pad_pre`/`pad_post` at 40/80/120/160 ms) is the obvious follow-up,
  and now that timing is measurable it can be settled rather than argued.
* Precision needs `--repeat 3` to mean anything, which triples the runtime. If that becomes a
  problem, pinning `cpu_threads=1` would make runs reproducible at the cost of speed.
* One episode, one language, one genre.

Conclusions that change behaviour belong in `PLAN.md` §14, not here.

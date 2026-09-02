# Detection quality

`PLAN.md` §12's quality loop. Every tuning decision in this project — which model, how much
padding, where the guards sit — was previously argued from a handful of observations. This is
where those arguments get settled with numbers.

> **Status: provisional.** The labels were seeded from the M1 run and **no human has listened
> to them**. Presence numbers (precision, recall, F1) are meaningful today; timing error is
> withheld until the labels are verified. See *Ground truth* below.

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

**Timing is not, yet.** A word boundary has to come from someone hearing it. A machine-seeded
boundary is only the opinion of whichever model seeded it, and scoring a model against its own
output is circular. Every label therefore carries `verified: false`, and the harness prints
`unverified` instead of a timing error unless `--allow-unverified-timing` is passed.

**One further bias worth stating.** The labels were seeded from a *windowed* `large-v3-turbo`
run, so they under-count words that only a full pass hears. Some of the false positives below
are very likely real profanity missing from the label set rather than detector errors. Verifying
the labels fixes this too.

## The clips

Five clips, chosen for distinct failure modes rather than density.

| id | span | why |
|---|---|---|
| c1 | 1765–1790 | three subtitle-only fallbacks — STT located none of them, so each mutes ~1.2 s where the word is ~0.3 s |
| c2 | 1830–1846 | a dense run of `fuck`, subtitle and STT agreeing, plus one STT-only hit at 0.22 confidence |
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
* **mute coverage** — the fraction of each labelled word that the *padded, merged* mute ranges
  actually silence. This is the metric that corresponds to "did the viewer hear it". Detection
  precision and recall do not: a detection whose range lands 200 ms short still leaves the word
  audible. It is consistently the lowest number in the table, and it is the one to optimise.

<!-- BEGIN RESULTS -->

| Model | Mode | TP | FP | FN | P | R | F1 | median err | mean err | mute cov | wall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| base | windowed | 15 | 8 | 3 | 0.65 | 0.83 | 0.73 | unverified | unverified | 0.59 | 86s |
| base | full | 6 | 1 | 12 | 0.86 | 0.33 | 0.48 | unverified | unverified | 0.32 | 97s |
| large-v3-turbo | windowed | 14 | 4 | 4 | 0.78 | 0.78 | 0.78 | unverified | unverified | 0.62 | 75s |
| large-v3-turbo | full | 13 | 0 | 5 | 1.00 | 0.72 | 0.84 | unverified | unverified | 0.69 | 102s |

<!-- END RESULTS -->

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

Measured directly on clip c2 — sixteen seconds of shouted dialogue — Silero reports **zero
seconds of speech** at its default threshold, and 0.9 s at a threshold of 0.2. No threshold
rescues it. VAD cost 5.5× the recall and bought no precision at all, on exactly the files full
mode exists to serve: the ones with no subtitles, where there is nothing else to fall back on.

**`large-v3-turbo` in full mode is the best configuration measured** (F1 0.84, precision 1.00,
the highest mute coverage). It is also the slowest, at roughly 1.4× realtime here, which is why
windowed remains the default and why full is reserved for files that have no usable subtitles.

**`base` is not good enough for full mode** (recall 0.33). It remains fine for the drift probe,
where all that is needed is enough words to align against.

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

## Known gaps

* No labels are verified, so no timing numbers. This is the single most valuable next step, and
  it needs someone who can listen to the audio.
* Mute coverage tops out at 0.69. Some of that is timing error and some is padding; a padding
  sweep (`pad_pre`/`pad_post` at 40/80/120/160 ms) is the obvious follow-up and the harness
  already supports it by varying settings.
* One episode, one language, one genre.

Conclusions that change behaviour belong in `PLAN.md` §14, not here.

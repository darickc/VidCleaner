"""The quality loop: precision, recall and timing error per model (PLAN.md §12).

Why this exists
===============

Every tuning decision in this project -- which model, how much padding, where the
guards sit -- has so far been argued from a handful of observations. §12 asks for
a labelled set so those arguments are settled with numbers instead.

What it can and cannot measure
==============================

**Presence** (precision and recall) rests on solid ground truth: the English
subtitles name the words that are spoken, so "is there a `fuck` in this clip" is
knowable without listening to it.

**Timing** does not. A label's word boundaries have to come from someone hearing
them, and a machine-seeded boundary is only ever the opinion of whichever model
seeded it -- measuring a model against its own output is circular. Labels
therefore carry ``verified: false`` until a human confirms them, and timing error
is withheld from the report unless ``--allow-unverified-timing`` says otherwise.
See ``docs/eval.md``.

Media is never committed: the labels reference one file by name and are useless
without it. That is the intended trade -- the timings are the valuable part and
they are tiny.

Usage::

    uv run python -m scripts.eval validate
    uv run python -m scripts.eval run    --media-dir ../video --models base,large-v3-turbo
    uv run python -m scripts.eval report --media-dir ../video --out ../docs/eval.md
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

LABELS_DIR = Path(__file__).resolve().parents[1] / "tests" / "eval" / "labels"
#: A detection matches a label when the canonical agrees and the spans are within
#: this of each other. Deliberately loose: it asks "did we find the word", which
#: is the precision/recall question. Timing error is reported separately, exactly.
MATCH_TOLERANCE_S = 0.5


class EvalError(RuntimeError):
    pass


# -------------------------------------------------------------------- schema


@dataclass(frozen=True, slots=True)
class Label:
    start: float
    end: float
    word: str
    category: str = ""
    verified: bool = False
    note: str = ""

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2.0


@dataclass(frozen=True, slots=True)
class Clip:
    id: str
    start: float
    end: float
    note: str = ""
    labels: tuple[Label, ...] = ()
    negatives: tuple[dict, ...] = ()
    """Spans that must NOT be muted -- reverent "God", "Scunthorpe" and so on.

    A clip with no labels at all is the most valuable kind: precision computed
    only over labelled regions cannot catch a detector that fires everywhere.
    """

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class LabelSet:
    name: str
    file: str
    duration_s: float
    clips: tuple[Clip, ...]
    path: Path | None = None

    @property
    def all_labels(self) -> list[Label]:
        return [label for clip in self.clips for label in clip.labels]

    @property
    def verified(self) -> bool:
        labels = self.all_labels
        return bool(labels) and all(label.verified for label in labels)


def load_label_set(path: Path) -> LabelSet:
    """Parse and validate one label file. Times are SOURCE CONTAINER TIME."""
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise EvalError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict) or "media" not in raw or "clips" not in raw:
        raise EvalError(f"{path}: expected top-level 'media' and 'clips'")

    media = raw["media"]
    clips: list[Clip] = []
    seen: set[str] = set()
    for entry in raw["clips"]:
        clip_id = str(entry.get("id") or "")
        if not clip_id:
            raise EvalError(f"{path}: every clip needs an id")
        if clip_id in seen:
            raise EvalError(f"{path}: duplicate clip id {clip_id!r}")
        seen.add(clip_id)
        start, end = float(entry["start"]), float(entry["end"])
        if end <= start:
            raise EvalError(f"{path}: clip {clip_id} ends before it starts")

        labels: list[Label] = []
        for item in entry.get("labels") or []:
            label = Label(
                start=float(item["start"]),
                end=float(item["end"]),
                word=str(item["word"]),
                category=str(item.get("category", "")),
                verified=bool(item.get("verified", False)),
                note=str(item.get("note", "")),
            )
            if not (start <= label.midpoint <= end):
                raise EvalError(
                    f"{path}: label {label.word!r} at {label.start} lies outside clip "
                    f"{clip_id} ({start}-{end}). Times are SOURCE time, not clip-relative."
                )
            labels.append(label)

        clips.append(
            Clip(
                id=clip_id,
                start=start,
                end=end,
                note=str(entry.get("note", "")),
                labels=tuple(sorted(labels, key=lambda x: x.start)),
                negatives=tuple(entry.get("negatives") or []),
            )
        )

    return LabelSet(
        name=str(media.get("name") or path.stem),
        file=str(media.get("file") or ""),
        duration_s=float(media.get("duration_s") or 0.0),
        clips=tuple(clips),
        path=path,
    )


def load_all(directory: Path = LABELS_DIR) -> list[LabelSet]:
    return [load_label_set(p) for p in sorted(directory.glob("*.yaml"))]


# ------------------------------------------------------------------- scoring


@dataclass
class Metrics:
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    negatives_violated: int = 0
    timing_errors: list[float] = field(default_factory=list)
    mute_coverage: list[float] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        found = self.true_positives + self.false_positives
        return self.true_positives / found if found else 1.0

    @property
    def recall(self) -> float:
        real = self.true_positives + self.false_negatives
        return self.true_positives / real if real else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def median_timing_error(self) -> float | None:
        return statistics.median(self.timing_errors) if self.timing_errors else None

    @property
    def mean_timing_error(self) -> float | None:
        """§12 asks for the mean. It is reported *beside* the median, not instead:
        M1 already saw a single 4.1 s whisperX span, and one outlier like that
        makes a mean describe the outlier rather than the model."""
        return statistics.fmean(self.timing_errors) if self.timing_errors else None

    @property
    def mean_mute_coverage(self) -> float | None:
        """The metric that corresponds to "did the viewer hear it".

        Detection-level precision and recall do not: a detection whose padded
        range lands 200 ms short still leaves the word audible.
        """
        return statistics.fmean(self.mute_coverage) if self.mute_coverage else None

    def merge(self, other: Metrics) -> Metrics:
        return Metrics(
            true_positives=self.true_positives + other.true_positives,
            false_positives=self.false_positives + other.false_positives,
            false_negatives=self.false_negatives + other.false_negatives,
            negatives_violated=self.negatives_violated + other.negatives_violated,
            timing_errors=self.timing_errors + other.timing_errors,
            mute_coverage=self.mute_coverage + other.mute_coverage,
            misses=self.misses + other.misses,
            spurious=self.spurious + other.spurious,
        )


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float, slack: float) -> bool:
    return a_start - slack < b_end and b_start - slack < a_end


def _covered(label: Label, mute_ranges) -> float:
    """Fraction of the labelled word the final mute ranges actually silence."""
    span = label.end - label.start
    if span <= 0:
        return 0.0
    covered = 0.0
    for rng in mute_ranges:
        overlap = min(label.end, rng.end) - max(label.start, rng.start)
        if overlap > 0:
            covered += overlap
    return min(1.0, covered / span)


def score(clip: Clip, detections, mute_ranges=(), *, tolerance_s: float = MATCH_TOLERANCE_S):
    """Compare one clip's detections against its labels. Pure.

    Matching is greedy nearest-midpoint and one-to-one, so two detections cannot
    both claim the same label and inflate recall.
    """
    metrics = Metrics()
    remaining = list(detections)

    for label in clip.labels:
        candidates = [
            d
            for d in remaining
            if d.word_canonical == label.word
            and _overlaps(d.start_s, d.end_s, label.start, label.end, tolerance_s)
        ]
        if not candidates:
            metrics.false_negatives += 1
            metrics.misses.append(f"{label.word} @ {label.start:.2f}")
            metrics.mute_coverage.append(0.0)
            continue
        best = min(candidates, key=lambda d: abs((d.start_s + d.end_s) / 2 - label.midpoint))
        remaining.remove(best)
        metrics.true_positives += 1
        metrics.timing_errors.append(abs(best.start_s - label.start))
        metrics.mute_coverage.append(_covered(label, mute_ranges))

    for detection in remaining:
        metrics.false_positives += 1
        metrics.spurious.append(f"{detection.word_canonical} @ {detection.start_s:.2f}")

    for negative in clip.negatives:
        start, end = float(negative["start"]), float(negative["end"])
        if any(_overlaps(r.start, r.end, start, end, 0.0) for r in mute_ranges):
            metrics.negatives_violated += 1
            metrics.spurious.append(f"MUTED A NEGATIVE @ {start:.2f}: {negative.get('note', '')}")

    return metrics


# ---------------------------------------------------------------- the harness


def resolve_media(label_set: LabelSet, media_dir: Path | None) -> Path | None:
    if media_dir is None:
        return None
    candidate = media_dir / label_set.file
    return candidate if candidate.is_file() else None


def cut_clip(source: Path, clip: Clip, dest_dir: Path) -> tuple[Path, float]:
    """Cut one clip so that its timeline starts exactly at ``clip.start``.

    Getting this wrong is silent and it looks like a detector bug. ``-ss`` placed
    *before* ``-i`` is an input seek, which with ``-c copy`` snaps back to the
    preceding keyframe -- measured here, up to 2.5 s early. Trusting the
    requested start put a systematic ~1.9 s error into every comparison and made
    a perfectly good detector score 0.21 precision.

    ``-copyts`` looked like the fix, since it keeps source timestamps, but it
    leaves the muxed subtitle timestamps rebased while the container duration
    becomes an absolute end time, so the clip disagrees with itself.

    What works is a *rough* input seek for speed followed by an **accurate output
    seek** for the remainder: the result is 0-based, exactly ``clip.duration``
    long, and its subtitle cues line up with its audio. Video is dropped -- these
    stages never render, and it makes the seek exact.
    """
    from vidcleaner.pipeline.ffmpeg import FFmpegRunner

    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{clip.id}.mkv"
    runner = FFmpegRunner(log_path=dest_dir / "ffmpeg.log")
    if not out.is_file():
        rough = max(0.0, clip.start - 15.0)
        # Map the English subtitle by index: `0:s:m:language:eng` is rejected by
        # ffmpeg 9, and the episode carries 61 subtitle streams.
        english = _english_subtitle_index(runner, source)
        maps = ["-map", "0:a:0"]
        if english is not None:
            maps += ["-map", f"0:s:{english}"]
        runner.run(
            [
                "-ss",
                str(rough),
                "-i",
                str(source),
                "-ss",
                str(clip.start - rough),
                "-t",
                str(clip.duration),
                *maps,
                "-c",
                "copy",
                str(out),
            ],
            label=f"cut-{clip.id}",
            timeout=900,
        )
    return out, clip.start


def _english_subtitle_index(runner, source: Path) -> int | None:
    """Typed index of the first English *text* subtitle stream, or None."""
    from vidcleaner.pipeline.artifacts import TEXT_SUBTITLE_CODECS

    typed = -1
    for stream in runner.probe(source, chapters=False).get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        typed += 1
        language = (stream.get("tags") or {}).get("language", "").lower()
        if language.startswith("en") and stream.get("codec_name") in TEXT_SUBTITLE_CODECS:
            return typed
    return None


def run_clip(clip_path: Path, *, model: str, stt_mode: str, work_dir: Path, shift: float):
    """Detect on one clip, returning times shifted back into episode time."""
    from vidcleaner.config import Settings, get_settings
    from vidcleaner.matching.compiler import build_matcher
    from vidcleaner.pipeline.artifacts import ProfileSnapshot
    from vidcleaner.pipeline.stages import DRY_RUN_STAGES, build_context, build_spec, run_pipeline
    from vidcleaner.settings_store import AppSettings

    matcher = build_matcher()
    settings = AppSettings(stt_windowed_model=model, stt_full_model=model, stt_full_max_hours=0)
    spec = build_spec(
        clip_path,
        profile=ProfileSnapshot(profile_hash=matcher.profile_hash),
        settings=settings,
        dry_run=True,
        stt_mode=stt_mode,
        # One work dir per cell, so models and modes never resume onto each other.
        job_id=f"eval-{clip_path.stem}-{model}-{stt_mode}".replace(".", "_"),
    )
    deploy = Settings(config_dir=get_settings().config_dir, work_dir=work_dir)
    ctx = build_context(spec, deploy=deploy, matcher=matcher)
    result = run_pipeline(ctx, list(DRY_RUN_STAGES))
    if result.detections is None:
        return [], []

    moved = [
        d.model_copy(update={"start_s": d.start_s + shift, "end_s": d.end_s + shift})
        for d in result.detections.detections
        if d.muted and not d.whitelisted
    ]
    ranges = [
        r.model_copy(update={"start": r.start + shift, "end": r.end + shift})
        for r in result.detections.mute_ranges
    ]
    return moved, ranges


# ----------------------------------------------------------------- reporting

_HEADERS = [
    ("model", "Model"),
    ("mode", "Mode"),
    ("tp", "TP"),
    ("fp", "FP"),
    ("fn", "FN"),
    ("precision", "P"),
    ("recall", "R"),
    ("f1", "F1"),
    ("median_err", "median err"),
    ("mean_err", "mean err"),
    ("coverage", "mute cov"),
    ("seconds", "wall"),
]


def format_table(rows: list[dict]) -> str:
    lines = [
        "| " + " | ".join(h for _, h in _HEADERS) + " |",
        "|" + "|".join("---" for _ in _HEADERS) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(key, "")) for key, _ in _HEADERS) + " |")
    return "\n".join(lines)


def _fmt(value, suffix="", places=3):
    return "n/a" if value is None else f"{value:.{places}f}{suffix}"


def write_report(path: Path, table: str) -> None:
    """Replace the generated block, leaving the hand-written prose alone."""
    marker_a, marker_b = "<!-- BEGIN RESULTS -->", "<!-- END RESULTS -->"
    body = f"{marker_a}\n\n{table}\n\n{marker_b}"
    if path.is_file():
        text = path.read_text()
        if marker_a in text and marker_b in text:
            head = text[: text.index(marker_a)]
            tail = text[text.index(marker_b) + len(marker_b) :]
            path.write_text(head + body + tail)
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VidCleaner detection quality harness")
    parser.add_argument("command", choices=("validate", "run", "report"))
    parser.add_argument("--media-dir", type=Path, help="directory holding the labelled media")
    parser.add_argument("--labels", type=Path, default=LABELS_DIR)
    parser.add_argument("--models", default="large-v3-turbo")
    parser.add_argument("--modes", default="windowed")
    parser.add_argument("--work-dir", type=Path, default=Path(".local/eval"))
    parser.add_argument("--out", type=Path, help="markdown file to update in place")
    parser.add_argument(
        "--allow-unverified-timing",
        action="store_true",
        help="report timing error even though no human has confirmed the labels",
    )
    args = parser.parse_args(argv)

    try:
        label_sets = load_all(args.labels)
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not label_sets:
        print(f"no label files in {args.labels}", file=sys.stderr)
        return 1

    if args.command == "validate":
        for label_set in label_sets:
            labels = label_set.all_labels
            verified = sum(1 for x in labels if x.verified)
            negatives = sum(len(c.negatives) for c in label_set.clips)
            print(
                f"{label_set.name}: {len(label_set.clips)} clips, {len(labels)} labels "
                f"({verified} verified), {negatives} must-not-mute regions, "
                f"media {label_set.file!r}"
            )
        return 0

    rows: list[dict] = []
    for label_set in label_sets:
        media = resolve_media(label_set, args.media_dir)
        if media is None:
            # A developer tool, not CI: a checkout without the media is normal.
            print(
                f"skipping {label_set.name}: {label_set.file!r} not found"
                f"{' under ' + str(args.media_dir) if args.media_dir else ''}."
                " Pass --media-dir to run it.",
                file=sys.stderr,
            )
            continue
        rows += _run_set(label_set, media, args)

    if not rows:
        print("nothing ran.", file=sys.stderr)
        return 0

    table = format_table(rows)
    print(table)
    for row in rows:
        metrics = row["_metrics"]
        if metrics.misses or metrics.spurious:
            print(f"\n{row['model']}/{row['mode']}:", file=sys.stderr)
            for miss in metrics.misses:
                print(f"  MISSED   {miss}", file=sys.stderr)
            for extra in metrics.spurious:
                print(f"  SPURIOUS {extra}", file=sys.stderr)
    if args.out:
        write_report(args.out, table)
        print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


def _run_set(label_set: LabelSet, media: Path, args) -> list[dict]:
    import time

    clips_dir = args.work_dir / "clips"
    cut = {clip.id: cut_clip(media, clip, clips_dir) for clip in label_set.clips}

    rows = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        for mode in [m.strip() for m in args.modes.split(",") if m.strip()]:
            started = time.monotonic()
            total = Metrics()
            for clip in label_set.clips:
                path, start = cut[clip.id]
                detections, ranges = run_clip(
                    path,
                    model=model,
                    stt_mode=mode,
                    work_dir=args.work_dir / "work",
                    shift=start,
                )
                total = total.merge(score(clip, detections, ranges))
            hide = not label_set.verified and not args.allow_unverified_timing
            rows.append(
                {
                    "model": model,
                    "mode": mode,
                    "tp": total.true_positives,
                    "fp": total.false_positives,
                    "fn": total.false_negatives,
                    "precision": _fmt(total.precision, places=2),
                    "recall": _fmt(total.recall, places=2),
                    "f1": _fmt(total.f1, places=2),
                    "median_err": "unverified" if hide else _fmt(total.median_timing_error, "s"),
                    "mean_err": "unverified" if hide else _fmt(total.mean_timing_error, "s"),
                    "coverage": _fmt(total.mean_mute_coverage, places=2),
                    "seconds": f"{time.monotonic() - started:.0f}s",
                    "_metrics": total,
                }
            )
            if total.negatives_violated:
                print(
                    f"!! {model}/{mode}: muted {total.negatives_violated} region(s) marked "
                    "must-not-mute",
                    file=sys.stderr,
                )
    return rows


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

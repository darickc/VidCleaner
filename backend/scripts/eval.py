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

REPO_ROOT = Path(__file__).resolve().parents[2]
LABELS_DIR = Path(__file__).resolve().parents[1] / "tests" / "eval" / "labels"
#: Where the clips you verify by hand are written. Deliberately **not** under
#: `.local/`: those files are opened in Audacity's file dialog, and a hidden
#: directory is not reachable from a GUI file picker. Gitignored -- it holds
#: audio cut from copyrighted media.
VERIFY_DIR = REPO_ROOT / "eval-clips"
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
    parser.add_argument(
        "command",
        choices=("validate", "run", "report", "verify", "export-audacity", "import-audacity"),
    )
    parser.add_argument("--media-dir", type=Path, help="directory holding the labelled media")
    parser.add_argument("--labels", type=Path, default=LABELS_DIR)
    parser.add_argument("--models", default="large-v3-turbo")
    parser.add_argument("--modes", default="windowed")
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(".local/eval"),
        help="scratch for cut clips and pipeline work dirs (machine-facing)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=VERIFY_DIR,
        help=f"where verify/export-audacity put files you open by hand (default {VERIFY_DIR})",
    )
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

    if args.command in ("verify", "export-audacity", "import-audacity"):
        return _verification_command(label_sets, args)

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


def _shell_quote(text: str) -> str:
    import shlex

    return shlex.quote(text)


def _verification_command(label_sets: list[LabelSet], args) -> int:
    """The half of §12 that needs a human: confirming word boundaries by ear."""
    for label_set in label_sets:
        media = resolve_media(label_set, args.media_dir)
        if media is None and args.command != "import-audacity":
            print(
                f"skipping {label_set.name}: {label_set.file!r} not found. Pass --media-dir.",
                file=sys.stderr,
            )
            continue
        target = args.dest / label_set.name

        if args.command == "verify":
            updated = verify_labels(label_set, media, target)
        elif args.command == "export-audacity":
            written = export_audacity(label_set, media, target)
            # Absolute, because the default work dir is a *hidden* directory and
            # a relative path in a message is not something anyone can act on.
            where = target.resolve()
            print(f"wrote {len(written)} files to:\n\n    {where}\n")
            for clip in label_set.clips:
                print(f"    {clip.id}.wav  +  {clip.id}.txt   ({len(clip.labels)} labels)")
            print(
                f"\nReveal them with:\n\n    open {_shell_quote(str(where))}\n\n"
                "In Audacity: open cN.wav, then File > Import > Labels for the matching "
                "cN.txt. Drag the boundaries against the waveform, then File > Export > "
                "Export Labels back over the same cN.txt and run:\n\n"
                "    uv run python -m scripts.eval import-audacity"
            )
            continue
        else:
            updated = import_audacity(label_set, target)

        path = save_label_set(updated)
        done = sum(1 for x in updated.all_labels if x.verified)
        print(f"wrote {path}: {done}/{len(updated.all_labels)} labels verified")
        if updated.verified:
            print("All labels verified -- `run` will now report timing error.")
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


# --------------------------------------------------------- label verification

VERIFY_CONTEXT_S = 1.5
NUDGE_S = 0.05


def _emit_yaml(label_set: LabelSet, header: str) -> str:
    """Re-emit a label file, preserving its header comment block.

    Hand-rolled rather than ``yaml.dump`` because the header explains the one
    clock rule and the verification status, and a round trip through PyYAML
    would silently delete all of it.
    """
    out = [header.rstrip(), "", "media:", f"  name: {label_set.name}"]
    out += [f'  file: "{label_set.file}"', f"  duration_s: {label_set.duration_s}", "", "clips:"]
    for clip in label_set.clips:
        out += [f"  - id: {clip.id}", f"    start: {clip.start}", f"    end: {clip.end}"]
        if clip.note:
            out.append("    note: >-")
            out += [f"      {line}" for line in _wrap(clip.note, 72)]
        if clip.labels:
            out.append("    labels:")
            for lab in clip.labels:
                note = lab.note.replace('"', "'")
                out.append(
                    f"      - {{start: {lab.start:.2f}, end: {lab.end:.2f}, word: {lab.word}, "
                    f"category: {lab.category}, verified: {str(lab.verified).lower()}, "
                    f'note: "{note}"}}'
                )
        else:
            out.append("    labels: []")
        if clip.negatives:
            out.append("    negatives:")
            for neg in clip.negatives:
                note = str(neg.get("note", "")).replace('"', "'")
                out.append(
                    f"      - {{start: {float(neg['start']):.2f}, "
                    f'end: {float(neg["end"]):.2f}, note: "{note}"}}'
                )
        out.append("")
    return "\n".join(out) + "\n"


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(" ".join(text.split()), width) or [""]


def _header_of(path: Path) -> str:
    lines = []
    for line in path.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            lines.append(line)
        else:
            break
    return "\n".join(lines)


def _snippet(runner, source: Path, start: float, end: float, dest: Path) -> Path:
    """Cut one span to a WAV so a plain player can play exactly it."""
    dest.unlink(missing_ok=True)
    runner.run(
        [
            "-ss",
            f"{max(0.0, start):.3f}",
            "-t",
            f"{max(0.05, end - start):.3f}",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "22050",
            "-f",
            "wav",
            str(dest),
        ],
        label="snippet",
        timeout=120,
    )
    return dest


def _play(path: Path) -> None:
    import shutil
    import subprocess

    player = shutil.which("afplay") or shutil.which("ffplay")
    if player is None:
        print("  (no afplay/ffplay on PATH -- open the file yourself)")
        return
    args = [player, str(path)]
    if player.endswith("ffplay"):
        args = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]
    subprocess.run(args, check=False)


VERIFY_HELP = """
  enter / y  accept these boundaries and mark the label verified
  r          replay just the labelled span
  c          replay with 1.5 s of context either side
  a / A      move the START 50 ms earlier / later
  z / Z      move the END 50 ms earlier / later
  w WORD     correct the word
  d          drop this label (it is not actually profanity here)
  s          skip, leaving it unverified
  q          save and quit
"""


def verify_labels(label_set: LabelSet, media: Path, work_dir: Path) -> LabelSet:
    """Play each unverified label and let a human fix its boundaries.

    This is the one part of §12 that cannot be automated: a word boundary has to
    come from someone hearing it, and a boundary seeded by a model makes the
    timing numbers circular.
    """
    import logging

    from vidcleaner.pipeline.ffmpeg import FFmpegRunner

    # This loop is a conversation with a person; a debug line per snippet would
    # scroll the prompt off the screen between every label.
    logging.disable(logging.INFO)
    work_dir.mkdir(parents=True, exist_ok=True)
    runner = FFmpegRunner(log_path=work_dir / "ffmpeg.log")
    span_wav, ctx_wav = work_dir / "_span.wav", work_dir / "_context.wav"

    todo = [(c, i) for c in label_set.clips for i, x in enumerate(c.labels) if not x.verified]
    print(f"{len(todo)} unverified label(s). {VERIFY_HELP}")

    updated = {c.id: list(c.labels) for c in label_set.clips}
    quit_now = False
    for position, (clip, index) in enumerate(todo, 1):
        if quit_now:
            break
        label = updated[clip.id][index]
        _snippet(runner, media, label.start, label.end, span_wav)
        print(
            f"\n[{position}/{len(todo)}] clip {clip.id}  {label.word!r}  "
            f"{label.start:.2f}-{label.end:.2f}  ({label.end - label.start:.2f}s)"
        )
        if label.note:
            print(f"          {label.note}")
        _play(span_wav)

        while True:
            try:
                answer = input("  > ").strip()
            except EOFError:
                quit_now = True
                break
            command = answer[:1].lower()
            if answer == "" or command == "y":
                label = _replace(label, verified=True, note="verified by ear")
                break
            if command == "q":
                quit_now = True
                break
            if command == "s":
                break
            if command == "d":
                updated[clip.id][index] = None
                break
            if command == "w" and len(answer) > 1:
                label = _replace(label, word=answer[1:].strip())
                print(f"  word is now {label.word!r}")
                continue
            if command == "r":
                _play(_snippet(runner, media, label.start, label.end, span_wav))
                continue
            if command == "c":
                _play(
                    _snippet(
                        runner,
                        media,
                        label.start - VERIFY_CONTEXT_S,
                        label.end + VERIFY_CONTEXT_S,
                        ctx_wav,
                    )
                )
                continue
            if answer in ("a", "A", "z", "Z"):
                delta = -NUDGE_S if answer in ("a", "z") else NUDGE_S
                if answer.lower() == "a":
                    label = _replace(label, start=round(label.start + delta, 3))
                else:
                    label = _replace(label, end=round(label.end + delta, 3))
                print(f"  {label.start:.2f}-{label.end:.2f} ({label.end - label.start:.2f}s)")
                _play(_snippet(runner, media, label.start, label.end, span_wav))
                continue
            print(VERIFY_HELP)
        updated[clip.id][index] = label if updated[clip.id][index] is not None else None

    clips = tuple(
        Clip(
            id=c.id,
            start=c.start,
            end=c.end,
            note=c.note,
            labels=tuple(x for x in updated[c.id] if x is not None),
            negatives=c.negatives,
        )
        for c in label_set.clips
    )
    return LabelSet(
        name=label_set.name,
        file=label_set.file,
        duration_s=label_set.duration_s,
        clips=clips,
        path=label_set.path,
    )


def _replace(label: Label, **kw) -> Label:
    from dataclasses import replace

    return replace(label, **kw)


# ------------------------------------------------------ Audacity round trip


def export_audacity(label_set: LabelSet, media: Path, dest: Path) -> list[Path]:
    """Write a WAV plus an Audacity label track per clip.

    Audacity's label format is three tab-separated fields -- start, end, text --
    with times relative to the file. Drag the boundaries against the waveform,
    File > Export > Export Labels over the same .txt, then run ``import-audacity``.
    Boundaries are far easier to place by eye on a waveform than by ear alone.
    """
    import logging

    from vidcleaner.pipeline.ffmpeg import FFmpegRunner

    # A debug line per snippet buries the path the person actually needs.
    logging.disable(logging.INFO)
    dest.mkdir(parents=True, exist_ok=True)
    runner = FFmpegRunner(log_path=dest / "ffmpeg.log")
    written = []
    for clip in label_set.clips:
        wav = dest / f"{clip.id}.wav"
        if not wav.is_file():
            _snippet(runner, media, clip.start, clip.end, wav)
        track = dest / f"{clip.id}.txt"
        track.write_text(
            "".join(
                f"{x.start - clip.start:.6f}\t{x.end - clip.start:.6f}\t{x.word}\n"
                for x in clip.labels
            )
        )
        written += [wav, track]
    return written


def import_audacity(label_set: LabelSet, source: Path) -> LabelSet:
    """Read corrected Audacity label tracks back, in source time."""
    clips = []
    for clip in label_set.clips:
        track = source / f"{clip.id}.txt"
        if not track.is_file():
            clips.append(clip)
            continue
        labels = []
        by_word = {x.word: x for x in clip.labels}
        for line in track.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            start, end, word = float(parts[0]), float(parts[1]), parts[2].strip()
            previous = by_word.get(word)
            labels.append(
                Label(
                    start=round(clip.start + start, 3),
                    end=round(clip.start + end, 3),
                    word=word,
                    category=previous.category if previous else "",
                    verified=True,
                    note="verified in Audacity",
                )
            )
        clips.append(
            Clip(
                id=clip.id,
                start=clip.start,
                end=clip.end,
                note=clip.note,
                labels=tuple(sorted(labels, key=lambda x: x.start)),
                negatives=clip.negatives,
            )
        )
    return LabelSet(
        name=label_set.name,
        file=label_set.file,
        duration_s=label_set.duration_s,
        clips=tuple(clips),
        path=label_set.path,
    )


def save_label_set(label_set: LabelSet) -> Path:
    assert label_set.path is not None
    header = _header_of(label_set.path)
    label_set.path.write_text(_emit_yaml(label_set, header))
    return label_set.path


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

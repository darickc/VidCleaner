"""OCR a PGS subtitle track into cues, to narrow STT windows (PLAN.md §11, M6).

A file whose only subtitles are bitmap gets ``no_text_subtitles_only_bitmap``
and is promoted to a full-file STT pass -- for a two-hour film with the
``medium`` model that is an hour or more, against roughly a minute for the
windowed path. Worse, a file longer than ``stt_full_max_hours`` is not promoted
at all and produces *no detections whatsoever*. OCR buys its way out of both.

Three rules make this safe, and they are the whole design:

1. **The output is windowing evidence, never library content.** The ``.srt``
   lands in ``/work/<job>/subs/`` and nothing else ever sees it. It cannot
   reach ``redactable_streams`` (which only filters ``probe.text_subtitles``,
   and a PGS stream is not one) nor ``find_sidecars`` (which looks beside the
   media file). So ``verify``'s fatal ``bitmap_subtitles_untouched`` check holds
   with no change to ``render`` or ``verify`` at all.
2. **OCR text never mutes on its own.** ``detect`` drops its 0.3-confidence
   "no STT token matched" fallback for an OCR source. For a human-authored cue
   that fallback means "Whisper missed it"; for OCR it may equally mean OCR
   invented the word, and the fallback silences ~1.2 s of real dialogue. See
   ``detect.DetectOptions.mute_subtitle_only``.
3. **Failure degrades to today.** No tesseract, an unreadable stream, or cues
   that turn out to be garbage all end at the existing full-file pass. The
   drift check is the backstop: wholesale nonsense collapses its coverage and
   ``subs.usable`` goes False, which is precisely the path a wrong subtitle
   file already takes.

``pytesseract`` and Pillow are imported inside functions, the same boundary
``tests/unit/test_no_stt_import.py`` enforces for torch, so ``pipeline`` still
imports on a checkout without the ``ocr`` extra.
"""

from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from vidcleaner.pipeline.artifacts import ProbeResult, SubtitleCue

NAME = "ocr"

__all__ = [
    "OcrStats",
    "extract_sup",
    "is_available",
    "ocr_sup",
    "resolve_workers",
]

#: Tesseract uses ISO 639-2/T codes, which is what we already store, so `eng`
#: passes straight through. Only the handful that differ need mapping.
_TESSERACT_LANGUAGE = {"gre": "ell", "cze": "ces", "dut": "nld", "ger": "deu", "fre": "fra"}


class OcrStats:
    """What the pass cost and how much it threw away, for the log and artifact."""

    __slots__ = ("cues_in", "cues_out", "dropped_empty", "dropped_confidence", "mean_confidence")

    def __init__(self) -> None:
        self.cues_in = 0
        self.cues_out = 0
        self.dropped_empty = 0
        self.dropped_confidence = 0
        self.mean_confidence = 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "cues_in": self.cues_in,
            "cues_out": self.cues_out,
            "dropped_empty": self.dropped_empty,
            "dropped_confidence": self.dropped_confidence,
            "mean_confidence": round(self.mean_confidence, 1),
        }


def is_available() -> bool:
    """Whether OCR can run at all.

    Checked rather than assumed because a missing binary must degrade to the
    existing behaviour, not fail a job: the container ships tesseract, but a
    developer checkout or a hand-built image may not have it.
    """
    if shutil.which("tesseract") is None:
        return False
    try:
        import pytesseract  # noqa: F401, PLC0415
        from PIL import Image  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def resolve_workers(configured: int = 0, *, cap: int = 4) -> int:
    """``0`` means "pick one", leaving room for the API and ffmpeg.

    Capped low on purpose: OCR runs while the worker is otherwise idle, but
    every tesseract process is a full core and this box is sized for STT.
    """
    if configured > 0:
        return configured
    cores = os.cpu_count() or 2
    return max(1, min(cap, cores - 2))


def extract_sup(ctx: Any, probe: ProbeResult, typed_index: int) -> Path:
    """Copy one PGS stream out to a raw ``.sup`` in the work dir.

    ``-c:s copy`` into ffmpeg's ``sup`` muxer, which is bit-exact -- verified by
    extracting a generated fixture back to a byte-identical file. This is also
    what removes any need for ``mkvextract``/mkvtoolnix in the image.
    """
    target = ctx.ws.subs_dir / f"ocr_{typed_index}.sup"
    target.parent.mkdir(parents=True, exist_ok=True)
    ctx.runner.run(
        ["-i", probe.path, "-map", f"0:s:{typed_index}", "-c:s", "copy", "-f", "sup", str(target)],
        label=f"extract-pgs-{typed_index}",
        timeout=900,
    )
    return target


def ocr_sup(
    sup_path: Path,
    *,
    language: str = "eng",
    min_confidence: int = 60,
    workers: int = 0,
    on_progress: Any = None,
    stats: OcrStats | None = None,
) -> list[SubtitleCue]:
    """Every legible cue in a ``.sup``, in presentation order.

    Cues below ``min_confidence`` are dropped rather than kept, because a
    dropped cue costs at most a missed STT window while a wrong one is a
    candidate for muting the wrong second of audio. Recall we can afford to
    lose; precision we cannot.
    """
    from vidcleaner.pipeline import pgs  # noqa: PLC0415 - keeps the import graph flat

    stats = stats if stats is not None else OcrStats()
    display_sets = pgs.parse_sup(sup_path.read_bytes())
    pgs_frames = pgs.frames(display_sets)
    stats.cues_in = len(pgs_frames)
    if not pgs_frames:
        return []

    tess_lang = _TESSERACT_LANGUAGE.get(language, language)
    total = len(pgs_frames)
    done = 0
    results: list[tuple[float, float, str, float] | None] = [None] * total

    def work(position: int) -> None:
        frame = pgs_frames[position]
        image = pgs.render(frame.display_set)
        if image is None:
            return
        text, confidence = _read_image(image, tess_lang)
        if text:
            results[position] = (frame.start_s, frame.end_s, text, confidence)

    with ThreadPoolExecutor(max_workers=resolve_workers(workers)) as pool:
        for _ in pool.map(work, range(total)):
            done += 1
            if on_progress is not None and (done % 25 == 0 or done == total):
                on_progress(done / total)

    cues: list[SubtitleCue] = []
    confidences: list[float] = []
    for entry in results:
        if entry is None:
            stats.dropped_empty += 1
            continue
        start, end, text, confidence = entry
        if confidence < min_confidence:
            stats.dropped_confidence += 1
            continue
        cues.append(SubtitleCue(index=len(cues), start=start, end=end, text=text))
        confidences.append(confidence)
    stats.cues_out = len(cues)
    stats.mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return cues


def _read_image(image: Any, language: str) -> tuple[str, float]:
    """One rendered cue -> ``(text, mean word confidence)``.

    Cropped to its ink first: a cue occupies a band at the bottom of a 1920x1080
    canvas, and handing tesseract the whole frame is both slower and worse.
    ``image_to_data`` rather than ``image_to_string`` because the per-word
    confidence is the only signal we have for "this cue is noise".
    """
    import pytesseract  # noqa: PLC0415

    # The glyphs are dark on light, so ink is what is *below* the background.
    ink = image.point(lambda pixel: 255 - pixel).getbbox()
    cropped = image.crop(ink) if ink else image

    try:
        data = pytesseract.image_to_data(
            cropped,
            lang=language,
            config="--psm 6",
            output_type=pytesseract.Output.DICT,
        )
    except Exception:  # noqa: BLE001 - a single unreadable cue must not end the pass
        return "", 0.0

    words: list[str] = []
    confidences: list[float] = []
    for text, confidence in zip(data.get("text", []), data.get("conf", []), strict=False):
        cleaned = (text or "").strip()
        try:
            score = float(confidence)
        except (TypeError, ValueError):
            continue
        if not cleaned or score < 0:
            continue
        words.append(cleaned)
        confidences.append(score)

    if not words:
        return "", 0.0
    return " ".join(words), sum(confidences) / len(confidences)

"""Build the synthetic test media used by ``tests/integration/``.

Everything is generated from ffmpeg's own sources -- no network, no committed
binaries -- so a fresh checkout can run the integration tier as soon as ffmpeg
is present. Only three tiny text inputs are committed (``tests/fixtures/*.srt``
and ``chapters.ffmeta``), which stay reviewable in a diff.

Audio is a steady 1 kHz sine, so every unmuted window sits at a known level and
the muted windows are the only silence. That is what makes "silent inside the
range, unchanged outside" an unambiguous assertion.

Two things are deliberately NOT generated here and are covered by unit tests
over committed ffprobe JSON instead:

* **DTS/TrueHD sources.** ffmpeg's ``dca`` and ``truehd`` encoders are
  experimental, so building such media would test ffmpeg rather than us.
* **Bitmap subtitles.** ffmpeg refuses text-to-bitmap subtitle transcoding
  ("only possible from text to text or bitmap to bitmap"), so a PGS/VobSub
  stream cannot be synthesized without committing binary media. The
  "bitmap subtitles are copied, never redacted" rule is a pure decision in
  ``render``/``subtitles`` and is asserted there.

Usage::

    uv run python -m scripts.make_fixtures --dest /tmp/fixtures
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

FIXTURE_INPUTS = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
DURATION = 10
VIDEO = f"color=c=black:s=320x240:r=24:d={DURATION}"
TONE = f"sine=f=1000:d={DURATION}"
TONE_ALT = f"sine=f=440:d={DURATION}"

VIDEO_ARGS = [
    "-c:v",
    "libx264",
    "-preset",
    "ultrafast",
    "-tune",
    "zerolatency",
    "-pix_fmt",
    "yuv420p",
]


@dataclass(frozen=True, slots=True)
class FixtureSet:
    """Every generated fixture, by role."""

    root: Path
    sample_mkv: Path
    sample_mp4: Path
    offset_mkv: Path
    surround71_mkv: Path
    nolang_mkv: Path
    nosubs_mkv: Path
    drift_mkv: Path

    def all(self) -> list[Path]:
        return [
            self.sample_mkv,
            self.sample_mp4,
            self.offset_mkv,
            self.surround71_mkv,
            self.nolang_mkv,
            self.nosubs_mkv,
            self.drift_mkv,
        ]


class FixtureError(RuntimeError):
    pass


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found is None:
        raise FixtureError("ffmpeg is not on PATH")
    return found


def _run(args: list[str]) -> None:
    completed = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise FixtureError(
            f"ffmpeg failed ({completed.returncode}): {completed.stderr.strip()[-800:]}"
        )


def build_sample_mkv(dest: Path) -> Path:
    """The main fixture: 2 audio tracks, 2 text subtitles, 2 chapters.

    Two audio tracks matter -- with ``-map 0:a`` the cleaned track lands at
    output ``a:(1+K)``, so a single-audio fixture would let a hardcoded ``a:1``
    pass. The second track also carries non-``default`` dispositions, which the
    subtractive ``-disposition:a:N -default`` must preserve.
    """
    out = dest / "sample.mkv"
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            VIDEO,
            "-f",
            "lavfi",
            "-i",
            TONE,
            "-f",
            "lavfi",
            "-i",
            TONE_ALT,
            "-i",
            str(FIXTURE_INPUTS / "marked.srt"),
            "-i",
            str(FIXTURE_INPUTS / "marked.es.srt"),
            "-i",
            str(FIXTURE_INPUTS / "chapters.ffmeta"),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-map",
            "3:s",
            "-map",
            "4:s",
            "-map_chapters",
            "5",
            *VIDEO_ARGS,
            "-c:a:0",
            "ac3",
            "-b:a:0",
            "640k",
            "-ac:a:0",
            "6",
            "-c:a:1",
            "aac",
            "-b:a:1",
            "128k",
            "-ac:a:1",
            "2",
            "-c:s",
            "srt",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:0",
            "title=Surround 5.1",
            "-metadata:s:a:1",
            "language=eng",
            "-metadata:s:a:1",
            "title=Commentary",
            "-disposition:a:0",
            "default",
            "-disposition:a:1",
            "comment",
            "-metadata:s:s:0",
            "language=eng",
            "-metadata:s:s:0",
            "title=English",
            "-metadata:s:s:1",
            "language=spa",
            "-metadata:s:s:1",
            "title=Spanish",
            "-disposition:s:0",
            "default",
            "-metadata",
            "title=VidCleaner Fixture",
            str(out),
        ]
    )
    return out


def build_sample_mp4(dest: Path) -> Path:
    """MP4 with an embedded ``mov_text`` subtitle.

    ``mov_text`` cannot be copied into Matroska, so this is what proves the
    remux path transcodes it to ``srt`` instead of failing to write the header.
    """
    out = dest / "sample.mp4"
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            VIDEO,
            "-f",
            "lavfi",
            "-i",
            TONE,
            "-i",
            str(FIXTURE_INPUTS / "marked.srt"),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:s",
            *VIDEO_ARGS,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-c:s",
            "mov_text",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:s:0",
            "language=eng",
            str(out),
        ]
    )
    return out


def build_offset_mkv(dest: Path) -> Path:
    """Audio starting at +0.5 s, to exercise the ONE CLOCK offset handling."""
    out = dest / "sample_offset.mkv"
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            VIDEO,
            "-itsoffset",
            "0.5",
            "-f",
            "lavfi",
            "-i",
            TONE,
            "-map",
            "0:v",
            "-map",
            "1:a",
            *VIDEO_ARGS,
            "-c:a",
            "ac3",
            "-b:a",
            "448k",
            "-ac",
            "6",
            "-metadata:s:a:0",
            "language=eng",
            str(out),
        ]
    )
    return out


def build_surround71_mkv(dest: Path) -> Path:
    """8 channels, which no AC-3/E-AC-3 encoder can carry -> the FLAC branch."""
    out = dest / "sample_71.mkv"
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            VIDEO,
            "-f",
            "lavfi",
            "-i",
            TONE,
            "-map",
            "0:v",
            "-map",
            "1:a",
            *VIDEO_ARGS,
            "-c:a",
            "flac",
            "-ac",
            "8",
            "-metadata:s:a:0",
            "language=eng",
            str(out),
        ]
    )
    return out


def build_nolang_mkv(dest: Path) -> Path:
    """Audio with no ``language`` tag: the clean track must mirror the absence."""
    out = dest / "sample_nolang.mkv"
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            VIDEO,
            "-f",
            "lavfi",
            "-i",
            TONE,
            "-map",
            "0:v",
            "-map",
            "1:a",
            *VIDEO_ARGS,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            str(out),
        ]
    )
    return out


def build_nosubs_mkv(dest: Path) -> Path:
    """No subtitle stream at all: the file that forces a full-file STT pass.

    Deliberately separate from ``sample_nolang.mkv``, which also lacks subtitles
    but exists to test a missing *audio language* tag. A test that reads
    ``nosubs_mkv`` says what it means.
    """
    out = dest / "sample_nosubs.mkv"
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            VIDEO,
            "-f",
            "lavfi",
            "-i",
            TONE,
            "-map",
            "0:v",
            "-map",
            "1:a",
            *VIDEO_ARGS,
            "-c:a",
            "ac3",
            "-b:a",
            "192k",
            "-ac",
            "2",
            "-metadata:s:a:0",
            "language=eng",
            str(out),
        ]
    )
    return out


#: How far ``sample_drift.mkv``'s subtitles run ahead of its audio.
DRIFT_SHIFT_S = 2.0


def build_drift_mkv(dest: Path) -> Path:
    """``sample.mkv``'s subtitles, shifted so the cues no longer match the audio.

    ``-itsoffset`` on the subtitle input rather than a second committed SRT, so
    the shift is one number in one place and the fixture cannot drift out of
    step with the value the tests assert.
    """
    out = dest / "sample_drift.mkv"
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            VIDEO,
            "-f",
            "lavfi",
            "-i",
            TONE,
            "-itsoffset",
            str(DRIFT_SHIFT_S),
            "-i",
            str(FIXTURE_INPUTS / "marked.srt"),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:s",
            *VIDEO_ARGS,
            "-c:a",
            "ac3",
            "-b:a",
            "192k",
            "-ac",
            "2",
            "-c:s",
            "srt",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:s:0",
            "language=eng",
            str(out),
        ]
    )
    return out


def build_all(dest: Path) -> FixtureSet:
    dest.mkdir(parents=True, exist_ok=True)
    return FixtureSet(
        root=dest,
        sample_mkv=build_sample_mkv(dest),
        sample_mp4=build_sample_mp4(dest),
        offset_mkv=build_offset_mkv(dest),
        surround71_mkv=build_surround71_mkv(dest),
        nolang_mkv=build_nolang_mkv(dest),
        nosubs_mkv=build_nosubs_mkv(dest),
        drift_mkv=build_drift_mkv(dest),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate VidCleaner test media")
    parser.add_argument("--dest", type=Path, required=True, help="output directory")
    args = parser.parse_args(argv)
    try:
        fixtures = build_all(args.dest)
    except FixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in fixtures.all():
        print(f"{path}  ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

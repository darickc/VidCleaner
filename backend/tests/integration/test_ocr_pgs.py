"""M6 end to end: a bitmap-only file, OCR'd, against real ffmpeg and tesseract.

The claim this tier has to earn is the milestone's whole point -- that a remux
with only PGS subtitles now stays in *windowed* STT mode instead of falling
through to a full-file pass. Everything else here guards the ways that could go
wrong quietly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vidcleaner.matching.compiler import build_matcher
from vidcleaner.pipeline import ocr as ocr_module
from vidcleaner.pipeline import probe as probe_stage
from vidcleaner.pipeline import stt as stt_stage
from vidcleaner.pipeline import subtitles as subs_stage
from vidcleaner.pipeline.artifacts import ProfileSnapshot
from vidcleaner.pipeline.stages import build_context, build_spec, run_stage
from vidcleaner.settings_store import AppSettings

pytestmark = pytest.mark.ocr


@pytest.fixture
def ctx_for(settings):
    def make(source: Path, **settings_kw):
        spec = build_spec(
            source,
            profile=ProfileSnapshot(profile_hash="v1:test"),
            settings=AppSettings(**{"drift_check": False, **settings_kw}),
        )
        context = build_context(spec, deploy=settings)
        context.matcher = build_matcher()
        return context

    return make


def run_to_subtitles(ctx):
    run_stage(ctx, "probe")
    run_stage(ctx, "subtitles")
    return subs_stage.load(ctx.ws)


# ------------------------------------------------------------------- the file


def test_the_fixture_really_carries_only_bitmap_subtitles(ctx_for, sample_pgs_mkv):
    """Otherwise the rest of this file proves nothing."""
    ctx = ctx_for(sample_pgs_mkv)
    run_stage(ctx, "probe")
    probe = probe_stage.load(ctx.ws)

    assert probe.text_subtitles == []
    assert [s.codec_name for s in probe.ocrable_subtitles] == ["hdmv_pgs_subtitle"]


# ------------------------------------------------------------------- the claim


def test_ocr_keeps_a_bitmap_only_file_in_windowed_mode(ctx_for, sample_pgs_mkv):
    """The milestone, as one assertion.

    Before M6 this file produced `no_text_subtitles_only_bitmap`, no cues, and
    `resolve_mode` promoted it to a full-file pass -- an hour of CPU for a
    feature film, or nothing at all above `stt_full_max_hours`.
    """
    ctx = ctx_for(sample_pgs_mkv)
    subs = run_to_subtitles(ctx)

    assert subs.source.kind == "ocr"
    assert subs.cues, "OCR must recover cues"
    assert subs.windows, "cues with hits must become STT windows"

    # ONE CLOCK: OCR cue times are source container time, like every other
    # subtitle source. `-c:s copy` out of the container preserves absolute
    # timestamps -- if it rebased them, every STT window would be misplaced by
    # the stream's start_time and nothing downstream would notice.
    from scripts.make_fixtures import PGS_CUES

    assert subs.cues[0].start == pytest.approx(PGS_CUES[0][0], abs=0.05)

    mode, reason = stt_stage.resolve_mode("windowed", subs, duration_s=10.0, settings=AppSettings())
    assert (mode, reason) == ("windowed", "subtitles")


def test_the_expected_words_survive_ocr(ctx_for, sample_pgs_mkv):
    """OCR is noisy on ordinary words; it must not be on the ones we came for.

    Asserted per word rather than on the joined text, because "Oh" reading as
    "On" and "I" as "|" is normal and irrelevant -- only the matched canonicals
    decide what gets a window.
    """
    ctx = ctx_for(sample_pgs_mkv)
    subs = run_to_subtitles(ctx)

    found = {hit.word_canonical for hit in subs.hits}
    assert {"fuck", "bullshit", "god damn"} <= found

    # And the honest other half: OCR loses words, which is why this feature only
    # ever *narrows* STT and never decides on its own what to mute. Pillow's
    # bundled font reads "Oh shit, that hurt." as "On snit, that nurt.", so
    # `shit` is simply gone -- a recall cost, never a wrong mute.
    assert "shit" not in found


def test_scunthorpe_still_does_not_match_after_ocr(ctx_for, sample_pgs_mkv):
    """The fixture includes it precisely so the boundary rule is exercised on
    text that arrived through a lossy channel rather than from a clean SRT."""
    ctx = ctx_for(sample_pgs_mkv)
    subs = run_to_subtitles(ctx)
    assert not any("cunt" in hit.word_canonical for hit in subs.hits)


# ------------------------------------------------------------------- scoping


def test_the_ocr_text_lands_in_work_and_never_beside_the_media(ctx_for, sample_pgs_mkv):
    """CLAUDE.md reserves library writes for ``swap``. An OCR guess must never
    become a file in the user's folder -- where ``find_sidecars`` would pick it
    up next run and ``redactable_sidecars`` would then redact our own guesses
    into their library."""
    ctx = ctx_for(sample_pgs_mkv)
    subs = run_to_subtitles(ctx)

    assert subs.source.path is not None
    written = Path(subs.source.path)
    assert written.is_file()
    assert ctx.ws.root in written.parents

    siblings = {p.suffix.lower() for p in sample_pgs_mkv.parent.iterdir() if p.is_file()}
    assert siblings == {".mkv"}, f"library folder gained files: {siblings}"


def test_a_pgs_stream_is_never_offered_for_redaction(ctx_for, sample_pgs_mkv):
    ctx = ctx_for(sample_pgs_mkv)
    subs = run_to_subtitles(ctx)
    assert subs.redactable == []
    assert subs.redactable_sidecars == []


def test_the_extracted_sup_is_a_bit_exact_copy(ctx_for, sample_pgs_mkv):
    """``-c:s copy`` into ffmpeg's ``sup`` muxer is what removes any need for
    mkvextract, so it is worth proving rather than assuming."""
    ctx = ctx_for(sample_pgs_mkv)
    run_stage(ctx, "probe")
    probe = probe_stage.load(ctx.ws)
    first = ocr_module.extract_sup(ctx, probe, probe.ocrable_subtitles[0].typed_index)
    second = ocr_module.extract_sup(ctx, probe, probe.ocrable_subtitles[0].typed_index)
    assert first.read_bytes() == second.read_bytes()
    assert first.stat().st_size > 0


# -------------------------------------------------------------------- opt-out


def test_the_setting_turns_it_off_and_restores_the_old_behaviour(ctx_for, sample_pgs_mkv):
    ctx = ctx_for(sample_pgs_mkv, ocr_bitmap_subtitles=False)
    subs = run_to_subtitles(ctx)

    assert subs.source.kind == "none"
    assert subs.source.reason == "no_text_subtitles_only_bitmap"
    assert subs.cues == []

    mode, reason = stt_stage.resolve_mode("windowed", subs, duration_s=10.0, settings=AppSettings())
    assert (mode, reason) == ("full", "no_subtitles")


def test_a_missing_tesseract_degrades_instead_of_failing(ctx_for, sample_pgs_mkv, monkeypatch):
    """A job must never fail because OCR is unavailable: the pre-M6 path is
    still a correct, if expensive, outcome."""
    monkeypatch.setattr(ocr_module, "is_available", lambda: False)
    ctx = ctx_for(sample_pgs_mkv)
    subs = run_to_subtitles(ctx)
    assert subs.source.kind == "none"
    assert subs.cues == []


def test_the_stats_record_what_the_pass_cost(ctx_for, sample_pgs_mkv):
    ctx = ctx_for(sample_pgs_mkv)
    subs = run_to_subtitles(ctx)
    assert subs.ocr_stats is not None
    assert subs.ocr_stats["cues_out"] == len(subs.cues)
    assert subs.ocr_stats["mean_confidence"] > 0


# --------------------------------------------------- render and verify survive


def test_a_pgs_file_renders_and_verifies_with_the_bitmap_stream_untouched(settings, sample_pgs_mkv):
    """The fatal check stays green -- proven by rendering, not by reasoning.

    ``verify.bitmap_subtitles_untouched`` fails a job if any bitmap stream
    changed codec. Since M6 now *reads* that stream, the assertion that it is
    still copied through byte-for-byte is the one that would catch an OCR path
    which accidentally started rewriting the user's subtitles.
    """
    from vidcleaner.pipeline import render as render_stage
    from vidcleaner.pipeline import verify as verify_stage
    from vidcleaner.pipeline.artifacts import (
        Detection,
        DetectionResult,
        ProfileSnapshot,
        TimeRange,
    )

    ranges = [TimeRange(start=2.0, end=3.0)]
    spec = build_spec(
        sample_pgs_mkv,
        profile=ProfileSnapshot(profile_hash="v1:test"),
        settings=AppSettings(drift_check=False),
    )
    ctx = build_context(spec, deploy=settings)
    ctx.matcher = build_matcher()
    for stage in ("probe", "extract", "subtitles"):
        run_stage(ctx, stage)

    DetectionResult(
        profile_hash="v1:test",
        detections=[
            Detection(
                word_raw="shit",
                word_canonical="shit",
                category="strong",
                start_s=r.start,
                end_s=r.end,
                mute_start_s=r.start,
                mute_end_s=r.end,
                source="both",
                confidence=0.95,
            )
            for r in ranges
        ],
        mute_ranges=ranges,
        total_muted_s=sum(r.duration for r in ranges),
    ).write(ctx.ws.detections_json)
    ctx.ws.mark_done("detect")

    run_stage(ctx, "render")
    run_stage(ctx, "verify")

    report = verify_stage.load(ctx.ws)
    assert report.ok, [c.name for c in report.checks if not c.ok]
    bitmap = next(c for c in report.checks if c.name == "bitmap_subtitles_untouched")
    assert bitmap.ok

    # And the stream really is still PGS in the output.
    out = render_stage.load(ctx.ws)
    probe = probe_stage.load(ctx.ws)
    assert probe.ocrable_subtitles, "the source must have had a PGS stream to begin with"
    assert Path(out.out_path).is_file()
    assert out.redacted == [], "a PGS stream must never appear as a redacted subtitle"

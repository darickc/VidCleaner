"""Subtitle source selection, cue parsing, windows and redaction (PLAN.md §6.3, §7)."""

from __future__ import annotations

import json
from pathlib import Path

import pysubs2
import pytest

from vidcleaner.matching.compiler import ProfileSpec, WhitelistRule, build_matcher
from vidcleaner.pipeline.artifacts import ProbeResult, SubtitleCue
from vidcleaner.pipeline.probe import parse_probe
from vidcleaner.pipeline.subtitles import (
    REDACTABLE_LANGUAGES,
    choose_subtitle_source,
    cue_windows,
    find_hits,
    find_sidecars,
    parse_cues,
    redact_file,
    redact_line,
    redactable_streams,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROBES = FIXTURES / "probe"


@pytest.fixture
def matcher():
    return build_matcher()


def probe_fixture(name: str) -> ProbeResult:
    return parse_probe(
        json.loads((PROBES / f"{name}.json").read_text()),
        path=Path(f"/media/{name}.mkv"),
        size=1_000,
        mtime=1.0,
        preferred_language="eng",
    )


# ------------------------------------------------------------------- parsing


def test_parse_cues_reads_the_marked_fixture():
    cues = parse_cues(FIXTURES / "marked.srt")
    assert len(cues) == 6
    assert cues[0].text == "Oh shit, that hurt."
    assert cues[0].start == pytest.approx(0.5)
    assert cues[0].end == pytest.approx(1.5)


def test_parse_cues_strips_inline_markup():
    cues = parse_cues(FIXTURES / "marked.srt")
    assert cues[3].text == "God damn it.", "the <i> tags should not reach the matcher"


def test_parse_cues_skips_blank_events(tmp_path):
    path = tmp_path / "blank.srt"
    path.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n\n\n2\n00:00:03,000 --> 00:00:04,000\nShit\n",
        encoding="utf-8",
    )
    assert [c.text for c in parse_cues(path)] == ["Shit"]


def test_parse_cues_rejects_garbage(tmp_path):
    path = tmp_path / "bad.srt"
    path.write_bytes(b"\x00\x01\x02 not subtitles")
    with pytest.raises(ValueError, match="cannot parse subtitles"):
        parse_cues(path)


# ---------------------------------------------------------------------- hits


def test_find_hits_reports_canonicals_and_offsets(matcher):
    cues = parse_cues(FIXTURES / "marked.srt")
    hits = find_hits(cues, matcher)
    assert [h.word_canonical for h in hits] == [
        "shit",
        "fuck",
        "god damn",
        "bullshit",
        "shit",
    ]
    first = hits[0]
    assert first.char_start == 3 and first.char_end == 7
    assert first.word_raw == "shit"


def test_find_hits_skips_the_never_match_cue(matcher):
    cues = parse_cues(FIXTURES / "marked.srt")
    hits = find_hits(cues, matcher)
    assert all(h.cue_index != 2 for h in hits), "Scunthorpe must not match"


def test_find_hits_skips_mild_words_by_default(matcher):
    """`friggin'` is in the mild category, which the default profile leaves off."""
    cues = parse_cues(FIXTURES / "marked.srt")
    assert all(h.cue_index != 4 for h in find_hits(cues, matcher))


def test_hit_time_span_is_proportional_to_character_offset(matcher):
    cue = SubtitleCue(index=0, start=10.0, end=20.0, text="aaaaaaaaaa shit")
    (hit,) = find_hits([cue], matcher)
    # "shit" starts at char 11 of 15, so ~73% into a 10 s cue
    assert hit.start == pytest.approx(10.0 + 10.0 * (11 / 15), abs=0.01)
    assert hit.end == pytest.approx(10.0 + 10.0 * (15 / 15), abs=0.01)


def test_a_hit_early_in_a_cue_lands_early(matcher):
    cue = SubtitleCue(index=0, start=0.0, end=10.0, text="shit " + "a" * 95)
    (hit,) = find_hits([cue], matcher)
    assert hit.start < 1.0


def test_compound_reports_separately_from_its_parent(matcher):
    cue = SubtitleCue(index=0, start=0.0, end=1.0, text="Bullshit. Bull. Shit.")
    assert [h.word_canonical for h in find_hits([cue], matcher)] == ["bullshit", "shit"]


# ------------------------------------------------------------------ windows


def test_windows_pad_the_cue_and_merge():
    cues = [
        SubtitleCue(index=0, start=10.0, end=11.0, text="shit"),
        SubtitleCue(index=1, start=12.0, end=13.0, text="shit"),
    ]
    hits = [h for cue in cues for h in find_hits([cue], build_matcher())]
    windows = cue_windows(cues, hits, duration=100.0)
    assert len(windows) == 1, "cues 1 s apart should merge"
    assert windows[0].start == pytest.approx(8.5)
    assert windows[0].end == pytest.approx(14.5)


def test_distant_cues_stay_separate(matcher):
    cues = [
        SubtitleCue(index=0, start=10.0, end=11.0, text="shit"),
        SubtitleCue(index=1, start=60.0, end=61.0, text="shit"),
    ]
    hits = find_hits(cues, matcher)
    assert len(cue_windows(cues, hits, duration=100.0)) == 2


def test_windows_are_clamped_to_the_file(matcher):
    cues = [SubtitleCue(index=0, start=0.2, end=1.0, text="shit")]
    hits = find_hits(cues, matcher)
    windows = cue_windows(cues, hits, duration=2.0)
    assert windows[0].start == 0.0
    assert windows[0].end == 2.0


def test_windows_apply_a_drift_offset(matcher):
    """M2 fills `offset_s`; M1 always passes 0.0, but the seam is exercised."""
    cues = [SubtitleCue(index=0, start=10.0, end=11.0, text="shit")]
    hits = find_hits(cues, matcher)
    shifted = cue_windows(cues, hits, duration=100.0, offset_s=5.0)
    assert shifted[0].start == pytest.approx(13.5)


def test_no_hits_means_no_windows(matcher):
    cues = [SubtitleCue(index=0, start=1.0, end=2.0, text="perfectly clean dialogue")]
    assert cue_windows(cues, find_hits(cues, matcher)) == []


# ---------------------------------------------------------- source selection


def test_embedded_english_stream_is_chosen():
    source = choose_subtitle_source(probe_fixture("eac3_atmos_many_subs"), preferred_language="eng")
    assert source.kind == "embedded"
    assert source.language == "eng"
    assert source.reason == "embedded_preferred_language"


def test_forced_streams_are_not_preferred():
    probe = probe_fixture("eac3_atmos_many_subs")
    source = choose_subtitle_source(probe, preferred_language="eng")
    chosen = next(s for s in probe.subtitles if s.typed_index == source.stream_typed_index)
    assert not chosen.is_forced


def test_bitmap_only_file_reports_no_usable_subtitles():
    probe = probe_fixture("dts_hd_bitmap_subs")
    text_only = probe.model_copy(update={"subtitles": [s for s in probe.subtitles if s.is_bitmap]})
    source = choose_subtitle_source(text_only, preferred_language="eng")
    assert source.kind == "none"
    assert source.reason == "no_text_subtitles_only_bitmap"


def test_a_file_with_no_subtitles_at_all():
    probe = probe_fixture("offset_stream")
    source = choose_subtitle_source(probe, preferred_language="eng")
    assert source.kind == "none" and source.reason == "no_subtitles"


def test_a_preferred_language_sidecar_wins_over_embedded(tmp_path):
    probe = probe_fixture("eac3_atmos_many_subs")
    probe = probe.model_copy(update={"path": str(tmp_path / "movie.mkv")})
    sidecar = tmp_path / "movie.eng.srt"
    sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nShit\n", encoding="utf-8")

    source = choose_subtitle_source(
        probe, preferred_language="eng", sidecars=find_sidecars(Path(probe.path))
    )
    assert source.kind == "sidecar"
    assert source.path == str(sidecar)
    assert source.language == "eng"


def test_an_untagged_sidecar_is_accepted(tmp_path):
    probe = probe_fixture("offset_stream").model_copy(update={"path": str(tmp_path / "movie.mkv")})
    sidecar = tmp_path / "movie.srt"
    sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nShit\n", encoding="utf-8")
    source = choose_subtitle_source(
        probe, preferred_language="eng", sidecars=find_sidecars(Path(probe.path))
    )
    assert source.kind == "sidecar" and source.reason == "sidecar_untagged"


def test_find_sidecars_ignores_unrelated_files(tmp_path):
    (tmp_path / "movie.mkv").write_bytes(b"")
    (tmp_path / "movie.eng.srt").write_text("", encoding="utf-8")
    (tmp_path / "other.srt").write_text("", encoding="utf-8")
    (tmp_path / "movie.nfo").write_text("", encoding="utf-8")
    found = find_sidecars(tmp_path / "movie.mkv")
    assert [p.name for p in found] == ["movie.eng.srt"]


# ------------------------------------------------------ redactable selection


def test_only_english_streams_are_redactable():
    """The M1 media has 61 text subtitle streams; only English has a word list."""
    probe = probe_fixture("eac3_atmos_many_subs")
    redactable = redactable_streams(probe)
    languages = {s.language for s in probe.subtitles if s.typed_index in redactable}
    assert languages <= REDACTABLE_LANGUAGES
    assert len(redactable) < len(probe.text_subtitles)


def test_streams_without_a_language_tag_are_not_redacted():
    probe = probe_fixture("eac3_atmos_many_subs")
    untagged = {s.typed_index for s in probe.subtitles if s.language is None}
    assert untagged
    assert untagged.isdisjoint(redactable_streams(probe))


def test_bitmap_streams_are_never_redactable():
    probe = probe_fixture("dts_hd_bitmap_subs")
    bitmap = {s.typed_index for s in probe.subtitles if s.is_bitmap}
    assert bitmap.isdisjoint(redactable_streams(probe))


# ---------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Oh shit, that hurt.", "Oh ****, that hurt."),
        ("FUCKING go!", "******* go!"),
        ("Bullshit.", "********."),
        ("God damn it.", "*** **** it."),
        ("Nothing to see in Scunthorpe.", "Nothing to see in Scunthorpe."),
        ("He was friggin' tired.", "He was friggin' tired."),
    ],
)
def test_redact_line_masks_matches_only(matcher, raw, expected):
    assert redact_line(raw, matcher)[0] == expected


def test_redaction_preserves_ass_override_tags(matcher):
    raw = r"{\i1}Oh {\b1}shit{\b0} you"
    out, hits, dropped = redact_line(raw, matcher)
    assert out == r"{\i1}Oh {\b1}****{\b0} you"
    assert (hits, dropped) == (1, 0)


def test_redaction_preserves_the_newline_escape(matcher):
    out, _, _ = redact_line(r"Oh\Nshit", matcher)
    assert out == r"Oh\N****"


def test_a_phrase_cannot_span_a_newline_escape(matcher):
    """The same rule as detection: `\\N` is not a phrase separator."""
    out, _, _ = redact_line(r"god\Ndamn", matcher)
    assert out == r"***\Ndamn"


def test_a_tag_inside_a_match_is_dropped_and_counted(matcher):
    """Losing italics on one word beats shipping the word."""
    out, hits, dropped = redact_line(r"f{\i1}uck", matcher)
    assert out == "****"
    assert (hits, dropped) == (1, 1)


def test_mask_preserves_length_and_word_separators(matcher):
    out, _, _ = redact_line("God damn", matcher)
    assert len(out) == len("God damn")
    assert out == "*** ****"


def test_whitelisted_words_are_not_masked():
    m = build_matcher(whitelist=[WhitelistRule("shit")])
    assert redact_line("Oh shit", m)[0] == "Oh shit"


def test_context_whitelist_applies_to_redaction():
    m = build_matcher(
        None,
        ProfileSpec(categories=frozenset({"strong", "slurs", "sexual", "religious"})),
        [WhitelistRule("spic", context_text="spic and span")],
    )
    assert redact_line("It was spic and span", m)[0] == "It was spic and span"


def test_redact_file_round_trips_srt(tmp_path, matcher):
    dest = tmp_path / "clean.srt"
    stats = redact_file(FIXTURES / "marked.srt", dest, matcher)
    assert stats.cues_total == 6
    # 5 hits across 4 cues: cue 5 ("Bullshit. Bull. Shit.") contains two.
    assert stats.cues_changed == 4
    assert stats.hits == 5

    text = dest.read_text()
    assert "****" in text
    assert "Scunthorpe" in text
    assert "shit" not in text.lower().replace("bullshit", "")


def test_redact_file_preserves_timing(tmp_path, matcher):
    dest = tmp_path / "clean.srt"
    redact_file(FIXTURES / "marked.srt", dest, matcher)
    before = pysubs2.load(str(FIXTURES / "marked.srt"))
    after = pysubs2.load(str(dest))
    assert [(e.start, e.end) for e in before] == [(e.start, e.end) for e in after]


def test_redact_file_leaves_clean_files_untouched(tmp_path, matcher):
    source = tmp_path / "clean_in.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nPerfectly ordinary dialogue.\n", encoding="utf-8"
    )
    stats = redact_file(source, tmp_path / "out.srt", matcher)
    assert (stats.cues_changed, stats.hits) == (0, 0)

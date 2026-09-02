"""The STT abstraction. No torch required -- the real backend has its own test."""

from __future__ import annotations

from pathlib import Path

import pytest

from vidcleaner.matching.compiler import ProfileSpec, build_matcher
from vidcleaner.pipeline.artifacts import (
    ProfileSnapshot,
    SubtitleCue,
    SubtitleSource,
    SubtitlesResult,
    TimeRange,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from vidcleaner.pipeline.stt import (
    INITIAL_PROMPT_WORDS,
    ScriptedTranscriber,
    Transcriber,
    TranscribeRequest,
    build_initial_prompt,
    build_request,
    model_for_mode,
    resolve_threads,
)
from vidcleaner.settings_store import AppSettings


def transcript() -> Transcript:
    return Transcript(
        model="test",
        language="en",
        segments=[
            TranscriptSegment(
                start=0.0,
                end=30.0,
                text="one two three",
                words=[
                    TranscriptWord(word="one", start=1.0, end=1.5),
                    TranscriptWord(word="two", start=10.0, end=10.5),
                    TranscriptWord(word="three", start=25.0, end=25.5),
                ],
            )
        ],
    )


def request(**kw) -> TranscribeRequest:
    defaults = dict(audio_path=Path("/work/audio.wav"), model="base")
    return TranscribeRequest(**{**defaults, **kw})


# ------------------------------------------------------- ScriptedTranscriber


def test_scripted_transcriber_satisfies_the_protocol():
    assert isinstance(ScriptedTranscriber(transcript()), Transcriber)


def test_scripted_transcriber_replays_everything_without_windows():
    result = ScriptedTranscriber(transcript()).transcribe(request())
    assert [w.word for w in result.words] == ["one", "two", "three"]


def test_scripted_transcriber_honours_windows():
    """Filtering rather than bypassing keeps the windowing logic exercised."""
    result = ScriptedTranscriber(transcript()).transcribe(
        request(windows=[TimeRange(start=9.0, end=12.0)])
    )
    assert [w.word for w in result.words] == ["two"]


def test_scripted_transcriber_records_the_mode_and_windows():
    windows = [TimeRange(start=0.0, end=30.0)]
    result = ScriptedTranscriber(transcript()).transcribe(request(windows=windows, mode="audit"))
    assert result.mode == "audit"
    assert result.windows == windows


def test_scripted_transcriber_reports_progress():
    seen: list[float] = []
    ScriptedTranscriber(transcript()).transcribe(request(), on_progress=seen.append)
    assert seen == [1.0]


def test_scripted_transcriber_loads_from_a_file(tmp_path):
    path = transcript().write(tmp_path / "transcript.json")
    assert ScriptedTranscriber(path).transcribe(request()).word_count == 3


def test_scripted_transcriber_drops_empty_segments():
    result = ScriptedTranscriber(transcript()).transcribe(
        request(windows=[TimeRange(start=100.0, end=200.0)])
    )
    assert result.segments == []


# ------------------------------------------------------------------ requests


def test_total_window_seconds():
    req = request(windows=[TimeRange(start=0.0, end=5.0), TimeRange(start=10.0, end=12.0)])
    assert req.total_window_s == 7.0


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("windowed", "large-v3-turbo"), ("full", "medium"), ("audit", "medium"), ("drift", "small")],
)
def test_model_for_mode(mode, expected):
    assert model_for_mode(AppSettings(), mode) == expected


def test_resolve_threads_leaves_headroom():
    """§4: cores - 2, so the API and ffmpeg are not starved."""
    import os

    assert resolve_threads(0) == max(1, (os.cpu_count() or 4) - 2)
    assert resolve_threads(0) >= 1


def test_resolve_threads_honours_an_explicit_value():
    assert resolve_threads(4) == 4


# ------------------------------------------------------------------- prompt


def test_initial_prompt_lists_canonicals():
    prompt = build_initial_prompt(build_matcher())
    assert prompt is not None
    assert prompt.startswith("The following transcript may contain explicit language:")
    assert "fuck" in prompt


def test_initial_prompt_is_capped():
    """Whisper's prompt window is 224 tokens and a long prompt degrades output."""
    prompt = build_initial_prompt(build_matcher())
    assert prompt is not None
    assert prompt.count(",") < INITIAL_PROMPT_WORDS + 2


def test_initial_prompt_respects_a_custom_limit():
    prompt = build_initial_prompt(build_matcher(), limit=3)
    assert prompt is not None and prompt.count(",") == 2


def test_initial_prompt_excludes_phrases():
    prompt = build_initial_prompt(build_matcher(), limit=200)
    assert "son of a bitch" not in prompt


def test_initial_prompt_is_none_for_an_empty_profile():
    empty = build_matcher(profile=ProfileSpec(categories=frozenset()))
    assert build_initial_prompt(empty) is None


# ------------------------------------------------------------ build_request


class _Ctx:
    def __init__(self, tmp_path, **settings_kw):
        from vidcleaner.config import get_settings
        from vidcleaner.pipeline.workspace import Workspace

        self.ws = Workspace("j", tmp_path / "j").ensure()
        self.settings = AppSettings(**settings_kw)
        self.deploy = get_settings()
        self.matcher = build_matcher()
        self.spec = type("S", (), {"stt_mode": "windowed", "profile": ProfileSnapshot()})()


def _probe():
    from vidcleaner.pipeline.artifacts import AudioStreamInfo, CodecPlan, ProbeResult

    return ProbeResult(
        path="/media/in.mkv",
        size=1,
        mtime=1.0,
        duration=100.0,
        audio=[
            AudioStreamInfo(
                index=1,
                typed_index=0,
                codec_name="ac3",
                channels=6,
                language="eng",
                is_default=True,
                start_time=1.4,
            )
        ],
        clean_codec=CodecPlan(encoder="ac3", bit_rate=640_000, reason="r"),
    )


def _subs(windows=(), cues=1):
    """Subtitles that parsed. ``cues=0`` models a file with no subtitles at all,
    which ``resolve_mode`` treats very differently -- see ``test_stt_mode.py``.
    """
    return SubtitlesResult(
        source=SubtitleSource(kind="embedded", language="eng"),
        cues=[
            SubtitleCue(index=i, start=float(i) * 2, end=float(i) * 2 + 1.5, text="nothing here")
            for i in range(cues)
        ],
        windows=list(windows),
    )


def test_build_request_carries_the_stream_offset(settings, tmp_path):
    """The one place audio.wav time becomes container time."""
    ctx = _Ctx(tmp_path)
    req = build_request(ctx, _probe(), _subs([TimeRange(start=0.0, end=5.0)]), mode="windowed")
    assert req.time_offset_s == 1.4


def test_build_request_maps_the_language_to_iso639_1(settings, tmp_path):
    ctx = _Ctx(tmp_path)
    req = build_request(ctx, _probe(), _subs(), mode="windowed")
    assert req.language == "en"


def test_build_request_uses_windows_only_in_windowed_mode(settings, tmp_path):
    ctx = _Ctx(tmp_path)
    windows = [TimeRange(start=0.0, end=5.0)]
    assert build_request(ctx, _probe(), _subs(windows), mode="windowed").windows == windows
    assert build_request(ctx, _probe(), _subs(windows), mode="full").windows == []


def test_build_request_selects_the_model_for_the_mode(settings, tmp_path):
    ctx = _Ctx(tmp_path)
    assert build_request(ctx, _probe(), _subs(), mode="windowed").model == "large-v3-turbo"
    assert build_request(ctx, _probe(), _subs(), mode="full").model == "medium"


def test_build_request_includes_the_prompt_when_enabled(settings, tmp_path):
    ctx = _Ctx(tmp_path, initial_prompt_hint=True)
    assert build_request(ctx, _probe(), _subs(), mode="windowed").initial_prompt


def test_build_request_omits_the_prompt_when_disabled(settings, tmp_path):
    ctx = _Ctx(tmp_path, initial_prompt_hint=False)
    assert build_request(ctx, _probe(), _subs(), mode="windowed").initial_prompt is None


def test_build_request_points_the_cache_at_the_config_dir(settings, tmp_path):
    ctx = _Ctx(tmp_path)
    req = build_request(ctx, _probe(), _subs(), mode="windowed")
    assert req.model_cache_dir == settings.config_dir / "models"


# ---------------------------------------------------------------- the stage


def test_the_stage_writes_an_empty_transcript_when_there_are_no_windows(settings, tmp_path):
    """A file whose subtitles contain no hits needs no STT at all."""
    from vidcleaner.pipeline import stt as stt_stage
    from vidcleaner.pipeline.stages import build_context, build_spec

    source = tmp_path / "x.mkv"
    source.write_bytes(b"")
    spec = build_spec(source, profile=ProfileSnapshot(profile_hash="v1:x"), settings=AppSettings())
    ctx = build_context(spec, deploy=settings, runner=object())
    ctx.matcher = build_matcher()
    _probe().write(ctx.ws.probe_json)
    _subs().write(ctx.ws.subs_json)

    stt_stage.run(ctx)
    result = stt_stage.load(ctx.ws)
    assert result is not None
    assert result.word_count == 0
    assert result.audio_start_offset_s == 1.4


def test_the_stage_uses_an_injected_transcriber(settings, tmp_path):
    from vidcleaner.pipeline import stt as stt_stage
    from vidcleaner.pipeline.stages import build_context, build_spec

    source = tmp_path / "x.mkv"
    source.write_bytes(b"")
    spec = build_spec(source, profile=ProfileSnapshot(profile_hash="v1:x"), settings=AppSettings())
    ctx = build_context(spec, deploy=settings, runner=object())
    ctx.matcher = build_matcher()
    ctx.transcriber = ScriptedTranscriber(transcript())
    _probe().write(ctx.ws.probe_json)
    _subs([TimeRange(start=0.0, end=30.0)]).write(ctx.ws.subs_json)

    stt_stage.run(ctx)
    assert stt_stage.load(ctx.ws).word_count == 3

"""The cleaning pipeline.

Per CLAUDE.md, ffmpeg and ffprobe are invoked via subprocess *only* from this
package, and every stage is a pure function of its on-disk inputs in
``/work/<job_id>/`` that writes a ``<stage>.done`` marker when it succeeds.

Two invariants hold across the whole package:

**Pure core + thin wrapper.** Each stage module exports a pure function over
typed values plus a short ``run(ctx)`` that loads artifacts, calls the core and
writes artifacts. That is what keeps the codec policy, the filter-graph builder,
the detector and half of verification testable without ffmpeg or torch.

**One clock.** Every time value written to ``subs.json``, ``transcript.json`` and
``detections.json`` is in *source container time*. ``audio.wav`` is 0-based;
``stt`` is the only place that applies the audio stream's ``start_time``, and
``render`` is the only place that converts back. See ``artifacts`` for detail.
"""

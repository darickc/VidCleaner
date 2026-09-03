"""Serving review clips — the one route that maps a URL onto a filesystem path."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

JOB = "0f2f7c1e-1111-2222-3333-444455556666"


@pytest.fixture
def snippet(settings) -> Path:
    directory = settings.snippets_dir / JOB / "0003"
    directory.mkdir(parents=True)
    (directory / "orig.m4a").write_bytes(b"original")
    (directory / "clean.m4a").write_bytes(b"cleaned")
    (directory / "wave.png").write_bytes(b"\x89PNG")
    return directory


def test_a_clip_is_served_with_an_audio_content_type(client: TestClient, snippet) -> None:
    response = client.get(f"/api/media/snippets/{JOB}/0003/clean.m4a")
    assert response.status_code == 200
    assert response.content == b"cleaned"
    assert response.headers["content-type"] == "audio/mp4"


def test_the_waveform_is_served_as_a_png(client: TestClient, snippet) -> None:
    response = client.get(f"/api/media/snippets/{JOB}/0003/wave.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_a_missing_clip_is_a_404(client: TestClient, snippet) -> None:
    assert client.get(f"/api/media/snippets/{JOB}/0009/orig.m4a").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/media/snippets/../../vidcleaner.db/0000/orig.m4a",
        "/api/media/snippets/%2e%2e%2f%2e%2e/0000/orig.m4a",
        f"/api/media/snippets/{JOB}/0003/../../../vidcleaner.db",
        f"/api/media/snippets/{JOB}/0003/settings.json",
    ],
)
def test_nothing_outside_the_snippet_root_can_be_read(
    client: TestClient, snippet, path: str
) -> None:
    """Only three file names exist, and only under the root. Both guards are here
    because either one alone has a way to be wrong."""
    assert client.get(path).status_code == 404


def test_a_symlink_out_of_the_root_is_refused(client: TestClient, settings, snippet) -> None:
    """The name check passes and the file exists -- only the resolved-path check
    catches this one."""
    secret = settings.config_dir / "settings.json"
    secret.write_text("{}")
    escape = settings.snippets_dir / JOB / "0004"
    escape.mkdir(parents=True)
    (escape / "orig.m4a").symlink_to(secret)

    assert client.get(f"/api/media/snippets/{JOB}/0004/orig.m4a").status_code == 404

"""What the four M4 screens read (PLAN.md §9.1-§9.4). No ffmpeg, no worker."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.support.library import add_backup, add_detections, add_job, make_movie, make_series

# ------------------------------------------------------------------- the queue


def test_the_queue_separates_running_queued_and_finished(client: TestClient) -> None:
    _, episodes = make_series(episodes=3)
    running = add_job(episodes[0], state="rendering", stage="render")
    queued = add_job(episodes[1], state="queued")
    done = add_job(episodes[2], state="done", is_last=True)

    body = client.get("/api/jobs").json()
    assert [j["id"] for j in body["running"]] == [running]
    assert [j["id"] for j in body["queued"]] == [queued]
    assert [j["id"] for j in body["recent"]] == [done]
    assert body["queued_total"] == 1


def test_queued_jobs_are_listed_in_claim_order(client: TestClient) -> None:
    """Lower priority runs sooner (db/constants). A page that sorted the other way
    would tell the user the exact opposite of what happens next."""
    _, episodes = make_series(episodes=3)
    backfill = add_job(episodes[0], priority=200, age_s=300)
    webhook = add_job(episodes[1], priority=100, age_s=100)
    manual = add_job(episodes[2], priority=50, age_s=10)

    body = client.get("/api/jobs").json()
    assert [j["id"] for j in body["queued"]] == [manual, webhook, backfill]


def test_a_queue_row_names_the_episode(client: TestClient) -> None:
    _, episodes = make_series("The Wire", episodes=1)
    add_job(episodes[0], state="queued")

    row = client.get("/api/jobs").json()["queued"][0]
    assert row["item"]["label"] == "The Wire S01E01 — Episode 1"
    assert row["item"]["path"].endswith("S01E01.mkv")


def test_a_movie_row_is_labelled_with_its_year(client: TestClient) -> None:
    _, item_id = make_movie("Heat")
    add_job(item_id, state="queued")
    assert client.get("/api/jobs").json()["queued"][0]["item"]["label"] == "Heat (1999)"


def test_the_queue_reports_totals_beyond_the_page(client: TestClient) -> None:
    _, episodes = make_series(episodes=5)
    for item_id in episodes:
        add_job(item_id, state="queued")

    body = client.get("/api/jobs?queued_limit=2").json()
    assert len(body["queued"]) == 2
    assert body["queued_total"] == 5


def test_job_detail_carries_the_log_tail_and_timings(client: TestClient) -> None:
    _, episodes = make_series(episodes=1)
    job_id = add_job(
        episodes[0],
        state="done",
        is_last=True,
        logs=("claimed", "probe done", "swap done"),
        timings_json='{"probe": 0.5, "render": 12.25}',
        profile_snapshot_json='{"name": "Default", "categories": ["strong"]}',
    )
    add_detections(job_id, episodes[0], "shit", "fuck")

    body = client.get(f"/api/jobs/{job_id}").json()
    assert [line["msg"] for line in body["logs"]] == ["claimed", "probe done", "swap done"]
    assert body["timings"] == {"probe": 0.5, "render": 12.25}
    assert body["profile"]["categories"] == ["strong"]
    assert body["detections"] == 2


def test_job_detail_survives_unparseable_json_columns(client: TestClient) -> None:
    """`timings_json` is written by the worker; a truncated write must not 500 the
    page that would show the user why the worker died."""
    _, episodes = make_series(episodes=1)
    job_id = add_job(episodes[0], state="failed", timings_json="{oops", profile_snapshot_json="")

    body = client.get(f"/api/jobs/{job_id}").json()
    assert body["timings"] == {} and body["profile"] == {}


def test_an_unknown_job_is_a_404(client: TestClient) -> None:
    assert client.get("/api/jobs/nope").status_code == 404


def test_timestamps_leave_as_utc(client: TestClient) -> None:
    """The database stores naive UTC; unqualified it would render in local time."""
    _, episodes = make_series(episodes=1)
    add_job(episodes[0], state="queued")
    created = client.get("/api/jobs").json()["queued"][0]["created_at"]
    assert created.endswith("Z") or "+00:00" in created


# ----------------------------------------------------------------- the library


def test_titles_carry_their_clean_progress(client: TestClient) -> None:
    title_id, episodes = make_series(episodes=3)
    add_job(episodes[0], state="done", is_last=True)
    from vidcleaner.db.models import MediaItem
    from vidcleaner.db.session import session_scope

    with session_scope() as session:
        session.get(MediaItem, episodes[0]).status = "clean"
        session.get(MediaItem, episodes[1]).status = "failed"

    row = next(t for t in client.get("/api/library/titles").json()["titles"] if t["id"] == title_id)
    assert (row["item_count"], row["clean_count"]) == (3, 1)
    assert (row["failed_count"], row["pending_count"]) == (1, 1)


def test_deferred_files_are_counted_apart_from_pending(client: TestClient) -> None:
    """A series enabled since M7 arrives with every episode unselected; counting them
    as pending would read like a stuck backlog forever."""
    from vidcleaner.db.models import MediaItem
    from vidcleaner.db.session import session_scope

    title_id, episodes = make_series(episodes=3)
    with session_scope() as session:
        for item_id in episodes[:2]:
            session.get(MediaItem, item_id).skip_backfill = True

    row = next(t for t in client.get("/api/library/titles").json()["titles"] if t["id"] == title_id)
    assert (row["deferred_count"], row["pending_count"]) == (2, 1)

    items = client.get(f"/api/library/titles/{title_id}").json()["items"]
    assert sum(item["skip_backfill"] for item in items) == 2


def test_titles_can_be_filtered_by_kind_search_and_enabled(client: TestClient) -> None:
    make_series("Breaking Bad", arr_id=1, enabled=True)
    make_series("Better Call Saul", arr_id=2, enabled=False)
    make_movie("Breaking Point", arr_id=1)

    names = lambda body: sorted(t["title"] for t in body["titles"])  # noqa: E731
    assert names(client.get("/api/library/titles?kind=series").json()) == [
        "Better Call Saul",
        "Breaking Bad",
    ]
    assert names(client.get("/api/library/titles?q=break").json()) == [
        "Breaking Bad",
        "Breaking Point",
    ]
    assert names(client.get("/api/library/titles?kind=series&enabled=false").json()) == [
        "Better Call Saul"
    ]


def test_a_title_with_no_items_still_lists(client: TestClient) -> None:
    """The outer join matters: a freshly synced series has no files yet."""
    title_id, _ = make_series(episodes=0)
    row = next(t for t in client.get("/api/library/titles").json()["titles"] if t["id"] == title_id)
    assert row["item_count"] == 0


def test_the_title_page_rolls_up_words_across_its_episodes(client: TestClient) -> None:
    title_id, episodes = make_series(episodes=2)
    first = add_job(episodes[0], state="done", is_last=True)
    second = add_job(episodes[1], state="done", is_last=True)
    add_detections(first, episodes[0], "shit", "fuck")
    add_detections(second, episodes[1], "shit")

    body = client.get(f"/api/library/titles/{title_id}").json()
    assert {c["word_canonical"]: c["total"] for c in body["counts"]} == {"shit": 2, "fuck": 1}
    assert [i["detection_count"] for i in body["items"]] == [2, 1]


def test_only_the_last_job_counts_towards_a_rollup(client: TestClient) -> None:
    """A reprocess leaves the previous run's detections in place as its record."""
    title_id, episodes = make_series(episodes=1)
    old = add_job(episodes[0], state="done")
    new = add_job(episodes[0], state="done", is_last=True)
    add_detections(old, episodes[0], "shit", "shit", "shit")
    add_detections(new, episodes[0], "shit")

    body = client.get(f"/api/library/titles/{title_id}").json()
    assert [c["total"] for c in body["counts"]] == [1]


def test_whitelisted_hits_are_left_out_of_the_rollup(client: TestClient) -> None:
    title_id, episodes = make_series(episodes=1)
    job_id = add_job(episodes[0], state="done", is_last=True)
    add_detections(job_id, episodes[0], "shit")
    add_detections(job_id, episodes[0], "bass", whitelisted=True)

    body = client.get(f"/api/library/titles/{title_id}").json()
    assert [c["word_canonical"] for c in body["counts"]] == ["shit"]
    assert body["items"][0]["detection_count"] == 1


def test_episodes_are_listed_in_broadcast_order(client: TestClient) -> None:
    title_id, _ = make_series(episodes=3)
    body = client.get(f"/api/library/titles/{title_id}").json()
    assert [i["episode"] for i in body["items"]] == [1, 2, 3]


def test_an_unknown_title_is_a_404(client: TestClient) -> None:
    assert client.get("/api/library/titles/999").status_code == 404


# -------------------------------------------------------------------- an item


def test_the_item_page_shows_the_last_job_and_its_detections(client: TestClient) -> None:
    _, item_id = make_movie("Heat")
    job_id = add_job(item_id, state="done", is_last=True, stt_mode="windowed")
    add_detections(job_id, item_id, "shit", "fuck")

    body = client.get(f"/api/items/{item_id}").json()
    assert body["item"]["label"] == "Heat (1999)"
    assert body["job"]["id"] == job_id and body["job"]["stt_mode"] == "windowed"
    assert [d["word_canonical"] for d in body["detections"]] == ["shit", "fuck"]
    assert body["restorable"] is False


def test_an_earlier_run_can_be_inspected(client: TestClient) -> None:
    _, item_id = make_movie()
    old = add_job(item_id, state="done")
    new = add_job(item_id, state="done", is_last=True)
    add_detections(old, item_id, "damn", "damn")
    add_detections(new, item_id, "damn")

    latest = client.get(f"/api/items/{item_id}").json()
    assert len(latest["detections"]) == 1
    assert [j["id"] for j in latest["jobs"]] == [new, old], "newest first"

    earlier = client.get(f"/api/items/{item_id}?job_id={old}").json()
    assert len(earlier["detections"]) == 2


def test_asking_for_another_items_job_is_a_404(client: TestClient) -> None:
    _, episodes = make_series(episodes=2)
    other = add_job(episodes[1], state="done", is_last=True)
    assert client.get(f"/api/items/{episodes[0]}?job_id={other}").status_code == 404


def test_a_kept_backup_makes_the_item_restorable(client: TestClient) -> None:
    _, item_id = make_movie()
    add_backup(item_id)
    body = client.get(f"/api/items/{item_id}").json()
    assert body["restorable"] is True
    assert body["backups"][0]["state"] == "kept"


def test_a_purged_backup_does_not(client: TestClient) -> None:
    _, item_id = make_movie()
    add_backup(item_id, state="purged")
    assert client.get(f"/api/items/{item_id}").json()["restorable"] is False


def test_whitelist_entries_in_scope_are_returned(client: TestClient) -> None:
    title_id, item_id = make_movie()
    _, other = make_series(arr_id=9)
    from vidcleaner.db.models import WhitelistEntry
    from vidcleaner.db.session import session_scope

    with session_scope() as session:
        session.add(WhitelistEntry(scope="global", canonical_word="bass"))
        session.add(WhitelistEntry(scope="title", scope_id=title_id, canonical_word="damn"))
        session.add(WhitelistEntry(scope="item", scope_id=item_id, canonical_word="hell"))
        session.add(WhitelistEntry(scope="item", scope_id=other[0], canonical_word="crap"))

    body = client.get(f"/api/items/{item_id}").json()
    words = {w["canonical_word"] for w in body["whitelist"]}
    assert {"bass", "damn", "hell"} <= words, "global, this title, and this item"
    assert "crap" not in words, "another item's whitelist is not in scope"


def test_a_detection_offers_its_clips_only_when_they_exist(client: TestClient, settings) -> None:
    _, item_id = make_movie()
    job_id = add_job(item_id, state="done", is_last=True)
    add_detections(job_id, item_id, "shit", "fuck")

    made: Path = settings.snippets_dir / job_id / "0000"
    made.mkdir(parents=True)
    for name in ("orig.m4a", "clean.m4a", "wave.png"):
        (made / name).write_bytes(b"\0")

    detections = client.get(f"/api/items/{item_id}").json()["detections"]
    assert detections[0]["snippet"] == f"/api/media/snippets/{job_id}/0000"
    assert detections[1]["snippet"] is None, "no files on disk, so no play button"


def test_an_unknown_item_is_a_404(client: TestClient) -> None:
    assert client.get("/api/items/999").status_code == 404


# ------------------------------------------- the audit pass must not blank a page


def test_an_audit_pass_does_not_hide_the_run_that_found_things(client: TestClient) -> None:
    """Found by the M4 demo. The audit job short-circuits at `probe` with
    `already_clean` and *does* become `last_job_id` (its profile hash is what stops
    the sync re-enqueueing forever) -- so reading its empty detections would show an
    Item page with nothing on it for a file full of muted words."""
    _, item_id = make_movie(status="clean")
    cleaned = add_job(item_id, state="done", trigger="backfill", age_s=600)
    add_detections(cleaned, item_id, "shit", "fuck")
    add_job(item_id, state="already_clean", trigger="audit", is_last=True, age_s=10)

    body = client.get(f"/api/items/{item_id}").json()
    assert body["job"]["id"] == cleaned
    assert [d["word_canonical"] for d in body["detections"]] == ["shit", "fuck"]


def test_a_title_rollup_survives_an_audit_pass(client: TestClient) -> None:
    title_id, episodes = make_series(episodes=1)
    cleaned = add_job(episodes[0], state="done", age_s=600)
    add_detections(cleaned, episodes[0], "shit")
    add_job(episodes[0], state="already_clean", trigger="audit", is_last=True, age_s=10)

    body = client.get(f"/api/library/titles/{title_id}").json()
    assert [c["word_canonical"] for c in body["counts"]] == ["shit"]
    assert body["items"][0]["detection_count"] == 1


def test_a_dry_run_never_becomes_the_evidence_for_a_file(client: TestClient) -> None:
    """M5. A dry run reaches `detect` and writes a full set of detections, but changes
    nothing on disk -- so it must not describe the file.

    Two cases share this shape. The "dry run" button forces past `already_clean`, so on
    a cleaned episode it detects over the already-muted Clean track and would report far
    fewer words than the file really has. And M5's audit phase 1 is a dry run by
    construction, detecting against the *backup* rather than the library file.
    """
    _, item_id = make_movie(status="clean")
    cleaned = add_job(item_id, state="done", trigger="backfill", age_s=600)
    add_detections(cleaned, item_id, "shit", "fuck")
    preview = add_job(item_id, state="done", trigger="manual", dry_run=True, is_last=True, age_s=10)
    add_detections(preview, item_id, "fuck")

    body = client.get(f"/api/items/{item_id}").json()
    assert body["job"]["id"] == cleaned
    assert [d["word_canonical"] for d in body["detections"]] == ["shit", "fuck"]


def test_a_reprocess_that_finds_nothing_is_still_the_current_run(client: TestClient) -> None:
    """The mirror case: after whitelisting the only hit, the page must show the new
    empty run rather than reaching back to the run that still has detections."""
    _, item_id = make_movie(status="clean")
    old = add_job(item_id, state="done", age_s=600)
    add_detections(old, item_id, "bass")
    fresh = add_job(item_id, state="done", trigger="reprocess", is_last=True, age_s=10)

    body = client.get(f"/api/items/{item_id}").json()
    assert body["job"]["id"] == fresh
    assert body["detections"] == []

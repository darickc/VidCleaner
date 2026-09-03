"""The Words & Profiles API (PLAN.md §9.5).

The matcher itself is `test_compiler.py`'s subject and the YAML merge is
`test_wordlist_sync.py`'s; these pin the HTTP surface the M5 page reads and writes --
in particular the three refusals that exist because the underlying tables cannot
express what the obvious request would mean (deleting a built-in, deleting the default
profile, and two entries claiming one form).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.support.library import make_movie, make_series
from vidcleaner.db.models import Profile, WhitelistEntry
from vidcleaner.db.models import WordEntry as WordEntryRow
from vidcleaner.db.session import session_scope
from vidcleaner.matching.compiler import DEFAULT_CATEGORIES


def word_named(words: list[dict], canonical: str) -> dict:
    return next(w for w in words if w["canonical"] == canonical)


# ------------------------------------------------------------------------ words


def test_the_word_list_is_the_yaml_merge_with_row_ids(client: TestClient) -> None:
    """The page needs an id to PATCH; `effective_entries` drops it."""
    body = client.get("/api/words").json()

    assert body["categories"] == ["strong", "mild", "religious", "slurs", "sexual"]
    assert sorted(body["default_categories"]) == sorted(DEFAULT_CATEGORIES)
    assert len(body["words"]) > 100

    fuck = word_named(body["words"], "fuck")
    assert fuck["id"] is not None
    assert fuck["category"] == "strong"
    assert fuck["enabled"] is True
    assert "fuckin'" in fuck["forms"]


def test_yaml_only_fields_survive_the_round_trip(client: TestClient) -> None:
    """`parent`, `note` and `focus` have no columns, so only the merge carries them."""
    words = client.get("/api/words").json()["words"]

    # A flattened compound keeps its parent, which is what the page nests under.
    assert word_named(words, "motherfucker")["parent"] == "fuck"
    # An entry shipped disabled must say why (the loader enforces the note).
    cock = word_named(words, "cock")
    assert cock["enabled"] is False
    assert cock["note"]
    # `focus` mutes one word of a phrase rather than the whole span.
    assert word_named(words, "son of a bitch")["focus"] == ["bitch"]


def test_counts_are_reported_per_category(client: TestClient) -> None:
    body = client.get("/api/words").json()
    assert sum(body["counts"].values()) == len(body["words"])
    # 50 of the shipped entries are off for precision, so the two differ.
    assert sum(body["enabled_counts"].values()) < sum(body["counts"].values())


def test_toggling_a_builtin_sticks(client: TestClient) -> None:
    words = client.get("/api/words").json()["words"]
    bloody = word_named(words, "bloody")
    assert bloody["enabled"] is False

    patched = client.patch(f"/api/words/{bloody['id']}", json={"enabled": True}).json()
    assert patched["enabled"] is True
    assert patched["note"], "the YAML note must survive a toggle"
    assert word_named(client.get("/api/words").json()["words"], "bloody")["enabled"] is True


def test_toggling_an_unknown_entry_is_404(client: TestClient) -> None:
    assert client.patch("/api/words/999999", json={"enabled": True}).status_code == 404


def test_a_custom_word_is_created_enabled_and_not_builtin(client: TestClient) -> None:
    created = client.post(
        "/api/words",
        json={"canonical": "frak", "category": "mild", "forms": ["fraks", "fraking"]},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["is_builtin"] is False
    assert body["enabled"] is True
    assert set(body["forms"]) == {"frak", "fraks", "fraking"}

    with session_scope() as session:
        row = session.scalars(select(WordEntryRow).where(WordEntryRow.canonical == "frak")).one()
        assert row.is_builtin is False


def test_a_custom_phrase_is_detected_from_its_canonical(client: TestClient) -> None:
    body = client.post("/api/words", json={"canonical": "gorram it", "category": "mild"}).json()
    assert body["is_phrase"] is True


def test_a_custom_word_is_validated_with_the_yaml_rules(client: TestClient) -> None:
    """A custom entry the loader would reject would behave unlike every built-in."""
    assert client.post("/api/words", json={"canonical": "Frak!"}).status_code == 422
    assert client.post("/api/words", json={"canonical": "a"}).status_code == 422
    # A multi-word form on a single-word entry: the separator only applies to phrases.
    bad = client.post("/api/words", json={"canonical": "frak", "forms": ["frak off"]})
    assert bad.status_code == 422


def test_a_custom_word_cannot_be_on_the_never_match_list(client: TestClient) -> None:
    """`never_match.yaml` exists because no regex separates `x-ray` from `f-ing`."""
    refused = client.post("/api/words", json={"canonical": "class"})
    assert refused.status_code == 422
    assert "never-match" in refused.json()["detail"]


def test_a_duplicate_canonical_is_refused(client: TestClient) -> None:
    assert client.post("/api/words", json={"canonical": "fuck"}).status_code == 409


def test_a_form_claimed_by_another_entry_is_refused(client: TestClient) -> None:
    """Two entries owning one form makes the form -> canonical map order-dependent."""
    refused = client.post("/api/words", json={"canonical": "frak", "forms": ["fucking"]})
    assert refused.status_code == 409
    assert "fucking" in refused.json()["detail"]


def test_a_custom_word_can_be_deleted(client: TestClient) -> None:
    word_id = client.post("/api/words", json={"canonical": "frak"}).json()["id"]
    assert client.delete(f"/api/words/{word_id}").status_code == 204
    assert all(w["canonical"] != "frak" for w in client.get("/api/words").json()["words"])


def test_a_builtin_cannot_be_deleted(client: TestClient) -> None:
    """Startup re-seeds the YAML, so the row would come back on the next restart --
    the button would appear to work and then silently undo itself."""
    words = client.get("/api/words").json()["words"]
    refused = client.delete(f"/api/words/{word_named(words, 'fuck')['id']}")
    assert refused.status_code == 409
    assert "disable" in refused.json()["detail"]


# --------------------------------------------------------------------- profiles


def test_the_seeded_default_profile_is_listed(client: TestClient) -> None:
    rows = client.get("/api/profiles").json()
    assert len(rows) == 1
    assert rows[0]["is_default"] is True
    assert sorted(rows[0]["categories"]) == sorted(DEFAULT_CATEGORIES)
    assert rows[0]["pad_pre_ms"] == 80
    assert rows[0]["pad_post_ms"] == 120


def test_a_profile_can_be_created_with_mild_enabled(client: TestClient) -> None:
    created = client.post(
        "/api/profiles",
        json={
            "name": "Strict",
            "categories": ["strong", "mild", "religious", "slurs", "sexual"],
            "pad_pre_ms": 120,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["is_default"] is False
    assert "mild" in body["categories"]
    assert body["pad_pre_ms"] == 120


def test_an_unknown_category_is_refused(client: TestClient) -> None:
    refused = client.post("/api/profiles", json={"name": "Bad", "categories": ["rude"]})
    assert refused.status_code == 422


def test_extra_word_ids_must_exist(client: TestClient) -> None:
    refused = client.post("/api/profiles", json={"name": "Bad", "extra_word_ids": [999999]})
    assert refused.status_code == 422


def test_a_duplicate_profile_name_is_refused(client: TestClient) -> None:
    client.post("/api/profiles", json={"name": "Strict"})
    assert client.post("/api/profiles", json={"name": "Strict"}).status_code == 409


def test_promoting_a_profile_demotes_the_previous_default(client: TestClient) -> None:
    """`profiles.is_default` has no unique constraint and `profile_spec` takes
    `.first()`, so two defaults would make the effective profile row-order dependent."""
    strict = client.post("/api/profiles", json={"name": "Strict"}).json()
    client.patch(f"/api/profiles/{strict['id']}", json={"is_default": True})

    rows = client.get("/api/profiles").json()
    assert [r["is_default"] for r in rows].count(True) == 1
    assert next(r for r in rows if r["is_default"])["name"] == "Strict"
    with session_scope() as session:
        defaults = session.scalars(select(Profile).where(Profile.is_default.is_(True))).all()
        assert len(defaults) == 1


def test_the_last_default_cannot_be_unset(client: TestClient) -> None:
    default = client.get("/api/profiles").json()[0]
    refused = client.patch(f"/api/profiles/{default['id']}", json={"is_default": False})
    assert refused.status_code == 422


def test_the_default_profile_cannot_be_deleted(client: TestClient) -> None:
    """Every title without an override resolves to it, so deleting it would silently
    change the mute set of the whole library."""
    default = client.get("/api/profiles").json()[0]
    refused = client.delete(f"/api/profiles/{default['id']}")
    assert refused.status_code == 409


def test_deleting_a_profile_resets_the_titles_that_used_it(client: TestClient) -> None:
    """`titles.profile_id` is ondelete=SET NULL, so they fall back to the default."""
    title_id, _ = make_series()
    strict = client.post("/api/profiles", json={"name": "Strict"}).json()
    client.patch(f"/api/library/titles/{title_id}", json={"profile_id": strict["id"]})

    assert client.get("/api/profiles").json()
    assert (
        next(r for r in client.get("/api/profiles").json() if r["id"] == strict["id"])["titles"]
        == 1
    )

    assert client.delete(f"/api/profiles/{strict['id']}").status_code == 204
    detail = client.get(f"/api/library/titles/{title_id}").json()
    assert detail["title"]["profile_id"] is None


def test_a_title_can_be_pointed_at_a_new_profile(client: TestClient) -> None:
    """§2's per-title override, end to end through the two endpoints the page uses."""
    title_id, _ = make_series()
    strict = client.post("/api/profiles", json={"name": "Strict"}).json()

    patched = client.patch(
        f"/api/library/titles/{title_id}", json={"profile_id": strict["id"]}
    ).json()
    assert patched["profile_id"] == strict["id"]
    assert client.get(f"/api/library/titles/{title_id}").json()["profile_name"] == "Strict"


# -------------------------------------------------------------------- whitelist


def test_the_whitelist_lists_every_scope_with_a_label(client: TestClient) -> None:
    """§9.4 shows only the rules in scope for one item; this page needs all of them."""
    title_id, episodes = make_series("Pluribus")
    client.post(
        f"/api/items/{episodes[0]}/whitelist",
        json={"canonical_word": "god", "scope": "item", "reprocess": False},
    )
    client.post(
        "/api/whitelist", json={"canonical_word": "hell", "scope": "title", "scope_id": title_id}
    )

    rows = client.get("/api/whitelist").json()
    by_word = {r["canonical_word"]: r for r in rows}
    assert by_word["god"]["scope"] == "item"
    assert "S01E01" in by_word["god"]["label"]
    assert by_word["hell"]["label"] == "Pluribus"
    # The shipped global seeds are here too, and carry no label.
    assert any(r["scope"] == "global" and r["label"] is None for r in rows)


def test_a_global_rule_needs_no_scope_id(client: TestClient) -> None:
    body = client.post("/api/whitelist", json={"canonical_word": "God"}).json()
    assert body["scope"] == "global"
    assert body["scope_id"] is None
    assert body["canonical_word"] == "god", "the word is normalized"


def test_a_scoped_rule_without_a_scope_id_is_refused(client: TestClient) -> None:
    refused = client.post("/api/whitelist", json={"canonical_word": "god", "scope": "title"})
    assert refused.status_code == 422
    assert "scope_id" in refused.json()["detail"]


def test_a_scoped_rule_must_point_at_something_real(client: TestClient) -> None:
    assert (
        client.post(
            "/api/whitelist",
            json={"canonical_word": "god", "scope": "title", "scope_id": 4242},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/whitelist",
            json={"canonical_word": "god", "scope": "item", "scope_id": 4242},
        ).status_code
        == 422
    )


def test_creating_the_same_rule_twice_does_not_duplicate_it(client: TestClient) -> None:
    first = client.post("/api/whitelist", json={"canonical_word": "god"}).json()
    second = client.post("/api/whitelist", json={"canonical_word": "god"}).json()
    assert first["id"] == second["id"]
    with session_scope() as session:
        rows = session.scalars(
            select(WhitelistEntry).where(
                WhitelistEntry.canonical_word == "god", WhitelistEntry.scope == "global"
            )
        ).all()
        assert len(rows) == 1


def test_the_words_page_rule_queues_nothing(client: TestClient) -> None:
    """A global rule can touch thousands of files; the user picks when that happens.
    ``POST /items/{id}/whitelist`` stays the flow that can queue a reprocess."""
    _title_id, item_id = make_movie()
    client.post("/api/whitelist", json={"canonical_word": "god"})

    body = client.get("/api/jobs").json()
    assert body["queued"] == []
    assert body["running"] == []
    assert item_id


def test_a_whitelist_rule_can_be_deleted(client: TestClient) -> None:
    entry_id = client.post("/api/whitelist", json={"canonical_word": "god"}).json()["id"]
    assert client.delete(f"/api/whitelist/{entry_id}").status_code == 204
    assert all(
        r["id"] != entry_id or r["scope"] != "global" for r in client.get("/api/whitelist").json()
    )


# --------------------------------------------------------------- whitelist mode


def test_a_rule_can_be_created_in_allow_mode(client: TestClient) -> None:
    """M5's negative form: `allow` cancels a broader `suppress` (§7's chain)."""
    title_id, _ = make_series()
    body = client.post(
        "/api/whitelist",
        json={
            "canonical_word": "god",
            "scope": "title",
            "scope_id": title_id,
            "mode": "allow",
        },
    ).json()
    assert body["mode"] == "allow"
    assert (
        next(r for r in client.get("/api/whitelist").json() if r["id"] == body["id"])["mode"]
        == "allow"
    )


def test_suppress_and_allow_for_one_word_are_separate_rows(client: TestClient) -> None:
    """They are not duplicates of each other: the pair *is* the override."""
    title_id, _ = make_series()
    a = client.post("/api/whitelist", json={"canonical_word": "god"}).json()
    b = client.post(
        "/api/whitelist",
        json={
            "canonical_word": "god",
            "scope": "title",
            "scope_id": title_id,
            "mode": "allow",
        },
    ).json()
    assert a["id"] != b["id"]


def test_an_unknown_mode_is_refused(client: TestClient) -> None:
    refused = client.post("/api/whitelist", json={"canonical_word": "god", "mode": "sometimes"})
    assert refused.status_code == 422


def test_the_item_page_reports_the_mode_of_a_rule_in_scope(client: TestClient) -> None:
    """§9.4 has to show it, or "why is this word still muted?" is unanswerable."""
    _title_id, episodes = make_series()
    client.post(
        f"/api/items/{episodes[0]}/whitelist",
        json={"canonical_word": "god", "scope": "item", "reprocess": False},
    )
    rows = client.get(f"/api/items/{episodes[0]}").json()["whitelist"]
    assert next(r for r in rows if r["canonical_word"] == "god")["mode"] == "suppress"

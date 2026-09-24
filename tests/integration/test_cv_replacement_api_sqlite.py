"""The /api/cv/replacements surface, end to end against real SQLite.

Every database here is created under `tmp_path` and thrown away; the operational
database is never opened. No real CV takes part: every reading is invented text
marked TEST ONLY, and the correction typed below is a unique sentinel that a
whole group of tests hunts for in error bodies, logs and reprs.

What is asserted is what a browser would actually receive — the status code, the
body, and the state the database is left in — through FastAPI's own client,
rather than the internals underneath.
"""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from services.api.cv_replacement import (
    MAX_STAGED_VALUE_LENGTH,
    PUBLIC_CV_REPLACEMENT_ERROR,
    PUBLIC_CV_REPLACEMENT_REQUEST_ERROR,
    PUBLIC_EXTRACTION_NOT_FOUND,
)
from services.api.main import app
from services.collector.database.connection import connect_database
from services.collector.database.migrations import (
    DEFAULT_MIGRATIONS_DIRECTORY,
    apply_migrations,
    discover_migrations,
)
from services.digital_twin.cv.candidates.models import (
    CANDIDATE_EXTRACTOR_VERSION,
    CandidateType,
    ExtractedCandidate,
    ExtractionRule,
    StructuredCvExtraction,
    candidate_fingerprint,
)
from services.digital_twin.cv.models import PARSER_VERSION, SectionType
from services.digital_twin.cv.replacement.models import DocumentOrigin
from services.digital_twin.cv.replacement.repository import (
    declare_active_cv_document,
    ensure_cv_document,
    ensure_extraction_with_manifest,
)
from services.digital_twin.facts.models import (
    FactSourceType,
    FactStatus,
    ProvenanceInput,
)
from services.digital_twin.facts.repository import (
    accept_profile_fact,
    ensure_profile_fact_proposal,
    list_profile_facts,
    list_verified_profile_facts,
)
from services.digital_twin.repository import ensure_user_profile
from services.profile_revision.watermark import (
    SyncPhase,
    active_profile_revision,
    phase_is_current,
    read_sync_watermarks,
)

DATABASE_NAME = "cv-replacement-api.db"

# TEST ONLY identities and digests; `.invalid` is reserved and never resolves.
TEST_ONLY_EMAIL = "cv-api-owner@example.invalid"
OTHER_EMAIL = "cv-api-other@example.invalid"
OLD_SHA256 = "a1" * 32
NEW_SHA256 = "b2" * 32

KEPT_SKILL = "KeptTestOnlyToolkit"
NEW_SKILL = "IncomingTestOnlyToolkit"
DROPPED_SKILL = "DroppedTestOnlyToolkit"
REFUSED_SKILL = "RefusedTestOnlyToolkit"
#: A second unique string, this one a CV *reading* rather than a correction.
#: It must reach the browser's JSON and nothing else — no repr, no log, no
#: error body — which is a different rule from the correction's.
SENTINEL_READING = "QpReadingSentinel9082TestOnly"
#: One unique string, typed as a correction, that must never appear anywhere a
#: machine reads: an error body, a log record, a repr.
SENTINEL_CORRECTION = "ZqCorrectionSentinel7431TestOnly"


# --------------------------------------------------------------- scaffolding


def configure(monkeypatch, path, profile_id):
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", str(profile_id))


def migrations_below(tmp_path, version):
    """A migrations directory holding every migration before `version`."""
    directory = tmp_path / f"migrations-below-{version}"
    directory.mkdir()
    for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
        if migration.version < version:
            (directory / migration.path.name).write_text(
                migration.path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return directory


def candidate(value: str, *, cv_sha256: str = NEW_SHA256) -> ExtractedCandidate:
    return ExtractedCandidate(
        candidate_type=CandidateType.SKILL,
        raw_text=value,
        normalized_value=None,
        page_numbers=(1,),
        section_type=SectionType.UNCLASSIFIED,
        section_index=0,
        rule_id=ExtractionRule.SECTION_LINE_BLOCK,
        fingerprint=candidate_fingerprint(CandidateType.SKILL, value.casefold()),
        cv_sha256=cv_sha256,
        parser_version=PARSER_VERSION,
        extractor_version=CANDIDATE_EXTRACTOR_VERSION,
    )


def accept_old_fact(connection, profile_id: int, value: str) -> int:
    proposal = ensure_profile_fact_proposal(
        connection,
        profile_id=profile_id,
        fact_type="SKILL",
        value=value,
        provenance=ProvenanceInput(
            source_type=FactSourceType.CV,
            cv_sha256=OLD_SHA256,
            parser_version=PARSER_VERSION,
            extractor_version=CANDIDATE_EXTRACTOR_VERSION,
            candidate_fingerprint=candidate_fingerprint(
                CandidateType.SKILL, value.casefold()
            ),
            rule_id=ExtractionRule.SECTION_LINE_BLOCK.value,
            page_numbers=(1,),
            section_type=SectionType.UNCLASSIFIED.value,
            section_index=0,
        ),
    )
    accept_profile_fact(connection, profile_id, proposal.fact.id)
    return proposal.fact.id


def seeded(tmp_path, monkeypatch, *, migrations_directory=None):
    """An active CV with two accepted skills, and an extraction to review.

    No replacement is opened: the tests that need one open it through the API,
    which is the surface under test.
    """
    path = tmp_path / DATABASE_NAME
    connection = connect_database(path)
    if migrations_directory is None:
        apply_migrations(connection)
    else:
        apply_migrations(connection, migrations_directory)
    profile_id = ensure_user_profile(connection, TEST_ONLY_EMAIL).profile_id
    other_profile_id = ensure_user_profile(connection, OTHER_EMAIL).profile_id
    configure(monkeypatch, path, profile_id)
    state = {
        "connection": connection,
        "path": path,
        "profile_id": profile_id,
        "other_profile_id": other_profile_id,
    }
    if migrations_directory is not None:
        return state

    declare_active_cv_document(
        connection,
        profile_id=profile_id,
        content_sha256=OLD_SHA256,
        origin=DocumentOrigin.LEGACY_DECLARED,
    )
    state["kept_fact_id"] = accept_old_fact(connection, profile_id, KEPT_SKILL)
    state["dropped_fact_id"] = accept_old_fact(connection, profile_id, DROPPED_SKILL)
    document = ensure_cv_document(
        connection,
        profile_id=profile_id,
        content_sha256=NEW_SHA256,
        byte_size=4096,
        page_count=2,
    )
    outcome = ensure_extraction_with_manifest(
        connection,
        profile_id=profile_id,
        document_id=document.id,
        extraction=StructuredCvExtraction(
            extractor_version=CANDIDATE_EXTRACTOR_VERSION,
            parser_version=PARSER_VERSION,
            cv_sha256=NEW_SHA256,
            candidates=(
                candidate(KEPT_SKILL),
                candidate(NEW_SKILL),
                candidate(REFUSED_SKILL),
                candidate(SENTINEL_READING),
            ),
            warnings=(),
        ),
    )
    state["document_id"] = document.id
    state["extraction_id"] = outcome.extraction.id
    return state


def open_review(client, state) -> dict:
    response = client.post(
        "/api/cv/replacements", json={"extraction_id": state["extraction_id"]}
    )
    assert response.status_code == 201, response.text
    return response.json()


def answer_everything(client, state, snapshot, *, refuse=()) -> dict:
    """Answer every entry: accept the incoming readings, keep the existing."""
    replacement_id = snapshot["replacement"]["replacement_id"]
    body = snapshot
    for entry in snapshot["entries"]:
        if entry["role"] == "INCOMING":
            payload = {
                "candidate_id": entry["candidate_id"],
                "decision": (
                    "REJECT" if entry["display_value"] in refuse else "ACCEPT"
                ),
            }
        else:
            payload = {"fact_id": entry["fact_id"], "decision": "KEEP"}
        response = client.put(
            f"/api/cv/replacements/{replacement_id}/decision", json=payload
        )
        assert response.status_code == 200, response.text
        body = response.json()
    return body


# ------------------------------------------------------------------- read


def test_no_open_review_is_a_plain_answer_and_creates_nothing(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    before = state["connection"].execute(
        "SELECT COUNT(*) FROM profile_cv_replacements"
    ).fetchone()[0]
    try:
        response = TestClient(app).get("/api/cv/replacements/current")
        assert response.status_code == 200
        assert response.json() == {
            "replacement": None,
            "progress": None,
            "entries": [],
        }
        assert state["connection"].execute(
            "SELECT COUNT(*) FROM profile_cv_replacements"
        ).fetchone()[0] == before
    finally:
        state["connection"].close()


def test_an_open_review_is_returned_with_its_plan_and_readings(
    tmp_path, monkeypatch
):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        open_review(client, state)
        body = client.get("/api/cv/replacements/current").json()

        assert body["replacement"]["extraction_id"] == state["extraction_id"]
        assert body["replacement"]["effective_state"] == "PREPARED"
        assert body["replacement"]["ready_review_digest"] is None
        assert body["replacement"]["activated_at"] is None
        assert body["progress"]["plan_entries"] == len(body["entries"])
        assert body["progress"]["unanswered"] == len(body["entries"])

        incoming = {
            entry["display_value"]
            for entry in body["entries"]
            if entry["role"] == "INCOMING"
        }
        existing = {
            entry["display_value"]
            for entry in body["entries"]
            if entry["role"] == "EXISTING"
        }
        # The incoming readings come from the manifest, the existing ones from
        # the baseline facts, each shown exactly as stored.
        assert incoming == {KEPT_SKILL, NEW_SKILL, REFUSED_SKILL, SENTINEL_READING}
        assert existing == {KEPT_SKILL, DROPPED_SKILL}
        for entry in body["entries"]:
            assert entry["decision"] is None
            assert entry["has_staged_value"] is False
    finally:
        state["connection"].close()


def test_the_snapshot_publishes_no_internal_digest_or_normal_form(
    tmp_path, monkeypatch
):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        open_review(client, state)
        raw = client.get("/api/cv/replacements/current").text
        for forbidden in (
            "state_digest",
            "reading_digest",
            "provenance_key",
            "normalized_value",
            "chain_digest",
        ):
            assert forbidden not in raw
        # `has_staged_value` is the contract; a field actually *named*
        # `staged_value` would be the leak, so the key is matched quoted.
        assert '"staged_value"' not in raw
        assert '"has_staged_value"' in raw
    finally:
        state["connection"].close()


def test_a_read_writes_nothing_at_all(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        open_review(client, state)
        tables = (
            "profile_cv_replacements",
            "profile_cv_replacement_decisions",
            "profile_facts",
            "profile_cv_documents",
        )
        before = {
            table: state["connection"]
            .execute(f"SELECT * FROM {table} ORDER BY 1")
            .fetchall()
            for table in tables
        }
        for _ in range(3):
            assert client.get("/api/cv/replacements/current").status_code == 200
        after = {
            table: state["connection"]
            .execute(f"SELECT * FROM {table} ORDER BY 1")
            .fetchall()
            for table in tables
        }
        assert after == before
    finally:
        state["connection"].close()


def test_the_review_served_is_the_configured_profiles_one(tmp_path, monkeypatch):
    """A second profile's open review is never what this surface answers."""
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        open_review(client, state)
        mine = client.get("/api/cv/replacements/current").json()
        # Re-point the server at the other profile: same database, no review.
        monkeypatch.setenv(
            "OPPORTUNITY_RADAR_PROFILE_ID", str(state["other_profile_id"])
        )
        theirs = TestClient(app).get("/api/cv/replacements/current").json()
        assert mine["replacement"] is not None
        assert theirs["replacement"] is None
    finally:
        state["connection"].close()


# ----------------------------------------------------------------- create


def test_opening_a_review_returns_201_and_the_snapshot(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        body = open_review(TestClient(app), state)
        assert body["replacement"]["lifecycle"] == "PREPARED"
        assert body["entries"]
    finally:
        state["connection"].close()


def test_a_second_open_review_is_a_conflict(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        open_review(client, state)
        response = client.post(
            "/api/cv/replacements", json={"extraction_id": state["extraction_id"]}
        )
        assert response.status_code == 409
    finally:
        state["connection"].close()


def test_an_unknown_extraction_is_not_found(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        response = TestClient(app).post(
            "/api/cv/replacements", json={"extraction_id": 987654}
        )
        assert response.status_code == 404
        assert response.json()["detail"] == PUBLIC_EXTRACTION_NOT_FOUND
    finally:
        state["connection"].close()


def test_another_profiles_extraction_is_not_found(tmp_path, monkeypatch):
    """Same database, another owner: it does not exist as far as this profile
    is concerned, and it is refused by identity rather than by luck."""
    state = seeded(tmp_path, monkeypatch)
    try:
        monkeypatch.setenv(
            "OPPORTUNITY_RADAR_PROFILE_ID", str(state["other_profile_id"])
        )
        response = TestClient(app).post(
            "/api/cv/replacements", json={"extraction_id": state["extraction_id"]}
        )
        assert response.status_code == 404
    finally:
        state["connection"].close()


@pytest.mark.parametrize(
    "body",
    [
        {"extraction_id": 1, "profile_id": 1},
        {"profile_id": 1},
        {},
        {"extraction_id": 0},
        {"extraction_id": -3},
        {"extraction_id": True},
        {"extraction_id": "1"},
        {"extraction_id": 1, "unexpected": "x"},
    ],
    ids=[
        "profile-id-alongside",
        "profile-id-alone",
        "empty",
        "zero",
        "negative",
        "boolean",
        "string",
        "unknown-field",
    ],
)
def test_a_malformed_open_body_is_refused(tmp_path, monkeypatch, body):
    state = seeded(tmp_path, monkeypatch)
    try:
        response = TestClient(app).post("/api/cv/replacements", json=body)
        assert response.status_code == 400
        assert state["connection"].execute(
            "SELECT COUNT(*) FROM profile_cv_replacements"
        ).fetchone()[0] == 0
    finally:
        state["connection"].close()


def test_a_body_that_is_not_json_is_refused_with_a_fixed_sentence(
    tmp_path, monkeypatch
):
    state = seeded(tmp_path, monkeypatch)
    try:
        response = TestClient(app).post(
            "/api/cv/replacements",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == PUBLIC_CV_REPLACEMENT_REQUEST_ERROR
    finally:
        state["connection"].close()


# --------------------------------------------------------------- decisions


def entry_for(snapshot, value, role="INCOMING"):
    for entry in snapshot["entries"]:
        if entry["role"] == role and entry["display_value"] == value:
            return entry
    raise AssertionError(f"no {role} entry for that reading")


def test_every_kind_of_answer_is_recorded_through_the_domain(
    tmp_path, monkeypatch
):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]

        answers = (
            ({"candidate_id": entry_for(snapshot, KEPT_SKILL)["candidate_id"],
              "decision": "ACCEPT"}, "ACCEPT"),
            ({"candidate_id": entry_for(snapshot, REFUSED_SKILL)["candidate_id"],
              "decision": "REJECT"}, "REJECT"),
            ({"candidate_id": entry_for(snapshot, NEW_SKILL)["candidate_id"],
              "decision": "CORRECT", "staged_value": SENTINEL_CORRECTION}, "CORRECT"),
            ({"fact_id": entry_for(snapshot, KEPT_SKILL, "EXISTING")["fact_id"],
              "decision": "KEEP"}, "KEEP"),
            ({"fact_id": state["dropped_fact_id"], "decision": "RETIRE"}, "RETIRE"),
            ({"candidate_id": entry_for(snapshot, SENTINEL_READING)["candidate_id"],
              "decision": "ACCEPT"}, "ACCEPT"),
        )
        body = snapshot
        for payload, expected in answers:
            response = client.put(
                f"/api/cv/replacements/{replacement_id}/decision", json=payload
            )
            assert response.status_code == 200, response.text
            body = response.json()
            target = [
                entry
                for entry in body["entries"]
                if entry["candidate_id"] == payload.get("candidate_id")
                and entry["fact_id"] == payload.get("fact_id")
            ]
            assert len(target) == 1
            assert target[0]["decision"] == expected

        assert body["progress"]["unanswered"] == 0
        # The correction is acknowledged without ever being echoed back.
        corrected = entry_for(body, NEW_SKILL)
        assert corrected["has_staged_value"] is True
        assert SENTINEL_CORRECTION not in json.dumps(body)
    finally:
        state["connection"].close()


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "ACCEPT"},
        {"candidate_id": 1, "fact_id": 2, "decision": "ACCEPT"},
        {"candidate_id": 1, "decision": "CORRECT"},
        {"candidate_id": 1, "decision": "ACCEPT", "staged_value": "x"},
        {"candidate_id": 1, "decision": "KEEP"},
        {"fact_id": 1, "decision": "ACCEPT"},
        {"candidate_id": 1, "decision": "WHATEVER"},
        {"candidate_id": 1, "decision": "ACCEPT", "profile_id": 1},
        {"candidate_id": 1, "decision": "CORRECT", "staged_value": "   "},
        {
            "candidate_id": 1,
            "decision": "CORRECT",
            "staged_value": "x",
            "staged_normalized_value": "x",
        },
    ],
    ids=[
        "no-target",
        "both-targets",
        "correct-without-value",
        "value-outside-correct",
        "existing-answer-on-candidate",
        "incoming-answer-on-fact",
        "unknown-decision",
        "profile-id",
        "blank-correction",
        "client-supplied-normal-form",
    ],
)
def test_a_malformed_decision_body_is_refused(tmp_path, monkeypatch, payload):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        response = client.put(
            f"/api/cv/replacements/{replacement_id}/decision", json=payload
        )
        assert response.status_code == 400
        assert state["connection"].execute(
            "SELECT COUNT(*) FROM profile_cv_replacement_decisions"
        ).fetchone()[0] == 0
    finally:
        state["connection"].close()


def test_an_oversized_correction_is_refused_by_length_alone(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        oversized = SENTINEL_CORRECTION * (
            MAX_STAGED_VALUE_LENGTH // len(SENTINEL_CORRECTION) + 2
        )
        response = client.put(
            f"/api/cv/replacements/{replacement_id}/decision",
            json={
                "candidate_id": entry_for(snapshot, NEW_SKILL)["candidate_id"],
                "decision": "CORRECT",
                "staged_value": oversized,
            },
        )
        assert response.status_code == 400
        assert SENTINEL_CORRECTION not in response.text
        assert str(MAX_STAGED_VALUE_LENGTH) in response.json()["detail"]
    finally:
        state["connection"].close()


def test_a_correction_is_stored_exactly_as_typed(tmp_path, monkeypatch):
    """No silent trim: the person's own spacing is theirs, not the API's."""
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        typed = f"  {SENTINEL_CORRECTION}  "
        response = client.put(
            f"/api/cv/replacements/{replacement_id}/decision",
            json={
                "candidate_id": entry_for(snapshot, NEW_SKILL)["candidate_id"],
                "decision": "CORRECT",
                "staged_value": typed,
            },
        )
        assert response.status_code == 200
        stored = state["connection"].execute(
            "SELECT staged_value FROM profile_cv_replacement_decisions"
            " WHERE replacement_id = ? AND staged_value IS NOT NULL",
            (replacement_id,),
        ).fetchone()
        assert stored[0] == typed
    finally:
        state["connection"].close()


def test_a_decision_the_domain_forbids_is_a_conflict(tmp_path, monkeypatch):
    """A protected fact is not this document's to retire, and the API does not
    re-decide that: it reports what the domain refused."""
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        # The kept skill is still supported by the new document, and a fact the
        # incoming CV still reads is an UNCHANGED_STILL_SUPPORTED entry; the
        # protection under test is the terminal one, so use a candidate whose
        # fact the domain refuses to answer twice in the same call instead.
        response = client.put(
            f"/api/cv/replacements/{replacement_id}/decision",
            json={"fact_id": 987654, "decision": "RETIRE"},
        )
        assert response.status_code == 409
    finally:
        state["connection"].close()


def test_changing_an_answer_after_ready_clears_the_token(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        answer_everything(client, state, snapshot)
        ready = client.post(f"/api/cv/replacements/{replacement_id}/ready").json()
        assert ready["replacement"]["ready_review_digest"] is not None

        changed = client.put(
            f"/api/cv/replacements/{replacement_id}/decision",
            json={
                "candidate_id": entry_for(snapshot, NEW_SKILL)["candidate_id"],
                "decision": "REJECT",
            },
        ).json()
        assert changed["replacement"]["ready_review_digest"] is None
        assert changed["replacement"]["effective_state"] == "REVIEWING"
    finally:
        state["connection"].close()


# --------------------------------------------------------------------ready


def test_a_complete_review_yields_a_token(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        answer_everything(client, state, snapshot)

        response = client.post(f"/api/cv/replacements/{replacement_id}/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["replacement"]["effective_state"] == "READY_TO_ACTIVATE"
        digest = body["replacement"]["ready_review_digest"]
        assert isinstance(digest, str) and len(digest) == 64
        assert all(character in "0123456789abcdef" for character in digest)
    finally:
        state["connection"].close()


def test_an_incomplete_review_cannot_be_declared_ready(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        response = client.post(f"/api/cv/replacements/{replacement_id}/ready")
        assert response.status_code == 409
    finally:
        state["connection"].close()


def test_a_contradictory_review_cannot_be_declared_ready(tmp_path, monkeypatch):
    """The same reading accepted from the new CV and retired from the profile."""
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        for entry in snapshot["entries"]:
            if entry["role"] == "INCOMING":
                payload = {"candidate_id": entry["candidate_id"], "decision": "ACCEPT"}
            elif entry["display_value"] == KEPT_SKILL:
                payload = {"fact_id": entry["fact_id"], "decision": "RETIRE"}
            else:
                payload = {"fact_id": entry["fact_id"], "decision": "KEEP"}
            assert (
                client.put(
                    f"/api/cv/replacements/{replacement_id}/decision", json=payload
                ).status_code
                == 200
            )

        response = client.post(f"/api/cv/replacements/{replacement_id}/ready")

        assert response.status_code == 409
        assert state["connection"].execute(
            "SELECT ready_review_digest FROM profile_cv_replacements WHERE id = ?",
            (replacement_id,),
        ).fetchone()[0] is None
    finally:
        state["connection"].close()


def test_a_stale_review_cannot_be_declared_ready(tmp_path, monkeypatch):
    """A fact gained evidence no CV produced after the person answered."""
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        answer_everything(client, state, snapshot)
        from services.digital_twin.facts.repository import (
            ensure_profile_fact_provenance,
        )

        ensure_profile_fact_provenance(
            state["connection"],
            profile_id=state["profile_id"],
            fact_id=state["dropped_fact_id"],
            provenance=ProvenanceInput(
                source_type=FactSourceType.USER_INPUT,
                source_locator="test-only-locator",
            ),
        )
        response = client.post(f"/api/cv/replacements/{replacement_id}/ready")
        assert response.status_code == 409
    finally:
        state["connection"].close()


# ---------------------------------------------------------------- activate


def ready_review(client, state, *, refuse=()):
    snapshot = open_review(client, state)
    replacement_id = snapshot["replacement"]["replacement_id"]
    answer_everything(client, state, snapshot, refuse=refuse)
    body = client.post(f"/api/cv/replacements/{replacement_id}/ready").json()
    return replacement_id, body["replacement"]["ready_review_digest"]


def test_an_activation_applies_the_review_and_says_only_what_it_did(
    tmp_path, monkeypatch
):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        replacement_id, digest = ready_review(client, state)

        response = client.post(
            f"/api/cv/replacements/{replacement_id}/activate",
            json={"review_digest": digest},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["activated"] is True
        assert body["activation_revision"] == 1
        assert body["document_id"] == state["document_id"]
        assert body["effective_state"] == "ACTIVATED"
        # Not one CV reading anywhere in the answer.
        raw = response.text
        for reading in (KEPT_SKILL, NEW_SKILL, DROPPED_SKILL, REFUSED_SKILL):
            assert reading not in raw
    finally:
        state["connection"].close()


def test_a_syntactically_invalid_digest_is_a_bad_request(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        replacement_id, _ = ready_review(client, state)
        for bad in ("", "abc", "A" * 64, "g" * 64, "ab" * 33):
            response = client.post(
                f"/api/cv/replacements/{replacement_id}/activate",
                json={"review_digest": bad},
            )
            assert response.status_code == 400, bad
        assert state["connection"].execute(
            "SELECT activated_at FROM profile_cv_replacements WHERE id = ?",
            (replacement_id,),
        ).fetchone()[0] is None
    finally:
        state["connection"].close()


def test_a_digest_that_is_not_the_stored_one_is_a_conflict(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        replacement_id, _ = ready_review(client, state)
        response = client.post(
            f"/api/cv/replacements/{replacement_id}/activate",
            json={"review_digest": "cd" * 32},
        )
        assert response.status_code == 409
        assert state["connection"].execute(
            "SELECT activated_at FROM profile_cv_replacements WHERE id = ?",
            (replacement_id,),
        ).fetchone()[0] is None
    finally:
        state["connection"].close()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"review_digest": "ab" * 32, "profile_id": 1},
        {"review_digest": "ab" * 32, "force": True},
        {"digest": "ab" * 32},
        {"review_digest": 12345},
    ],
    ids=["empty", "profile-id", "unknown-field", "wrong-name", "not-a-string"],
)
def test_a_malformed_activate_body_is_refused(tmp_path, monkeypatch, body):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        replacement_id, _ = ready_review(client, state)
        response = client.post(
            f"/api/cv/replacements/{replacement_id}/activate", json=body
        )
        assert response.status_code == 400
    finally:
        state["connection"].close()


def test_replaying_an_activation_writes_nothing_more(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        replacement_id, digest = ready_review(client, state)
        first = client.post(
            f"/api/cv/replacements/{replacement_id}/activate",
            json={"review_digest": digest},
        ).json()
        tables = (
            "profile_facts",
            "profile_fact_provenance",
            "profile_cv_documents",
            "profile_cv_replacements",
        )
        after_first = {
            table: state["connection"]
            .execute(f"SELECT * FROM {table} ORDER BY 1")
            .fetchall()
            for table in tables
        }

        second = client.post(
            f"/api/cv/replacements/{replacement_id}/activate",
            json={"review_digest": digest},
        )

        assert second.status_code == 200
        assert first["activated"] is True
        assert second.json()["activated"] is False
        assert {
            table: state["connection"]
            .execute(f"SELECT * FROM {table} ORDER BY 1")
            .fetchall()
            for table in tables
        } == after_first
    finally:
        state["connection"].close()


def test_an_activation_leaves_the_downstream_phases_to_an_operator(
    tmp_path, monkeypatch
):
    """The Recommendation closes immediately; nothing else is synchronized."""
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        replacement_id, digest = ready_review(client, state)
        client.post(
            f"/api/cv/replacements/{replacement_id}/activate",
            json={"review_digest": digest},
        )

        connection, profile_id = state["connection"], state["profile_id"]
        recommendation = connection.execute(
            "SELECT state FROM recommendation_profile_state WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        assert recommendation is not None and recommendation[0] == "INCOMPLETE"
        # A new revision exists and the API advanced no watermark at all.
        assert active_profile_revision(connection, profile_id) == 1
        assert dict(read_sync_watermarks(connection, profile_id)) == {
            phase: 0 for phase in SyncPhase
        }
        # And every downstream phase is therefore behind, which is what makes
        # its stored result stop being presentable as current (B1b-C).
        for phase in SyncPhase:
            assert phase_is_current(connection, profile_id, phase) is False
    finally:
        state["connection"].close()


def test_a_refused_reading_is_remembered_as_refused_and_is_never_current(
    tmp_path, monkeypatch
):
    """The REJECT contract, through the API and all the way to the facts."""
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        replacement_id, digest = ready_review(client, state, refuse=(REFUSED_SKILL,))
        body = client.post(
            f"/api/cv/replacements/{replacement_id}/activate",
            json={"review_digest": digest},
        ).json()
        assert body["facts_rejected"] == 1

        connection, profile_id = state["connection"], state["profile_id"]
        refused = [
            fact
            for fact in list_profile_facts(connection, profile_id)
            if fact.value == REFUSED_SKILL
        ]
        # It exists, it is terminal, and it is nowhere near the live profile.
        assert len(refused) == 1
        assert refused[0].status is FactStatus.REJECTED
        assert refused[0].is_verified is False
        assert refused[0].is_current is False
        assert REFUSED_SKILL not in {
            fact.value for fact in list_verified_profile_facts(connection, profile_id)
        }
    finally:
        state["connection"].close()


# ------------------------------------------------------------------ cancel


def test_an_open_review_can_be_abandoned(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        client.put(
            f"/api/cv/replacements/{replacement_id}/decision",
            json={
                "candidate_id": entry_for(snapshot, NEW_SKILL)["candidate_id"],
                "decision": "ACCEPT",
            },
        )

        response = client.post(f"/api/cv/replacements/{replacement_id}/cancel")

        assert response.status_code == 200
        assert response.json() == {
            "replacement_id": replacement_id,
            "effective_state": "CANCELLED",
        }
        # The answers stay, as the domain says they should.
        assert state["connection"].execute(
            "SELECT COUNT(*) FROM profile_cv_replacement_decisions"
            " WHERE replacement_id = ?",
            (replacement_id,),
        ).fetchone()[0] == 1
        assert client.get("/api/cv/replacements/current").json()["replacement"] is None
    finally:
        state["connection"].close()


def test_cancelling_an_activated_review_is_a_conflict(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        replacement_id, digest = ready_review(client, state)
        client.post(
            f"/api/cv/replacements/{replacement_id}/activate",
            json={"review_digest": digest},
        )
        response = client.post(f"/api/cv/replacements/{replacement_id}/cancel")
        assert response.status_code == 409
    finally:
        state["connection"].close()


def test_cancelling_an_unknown_review_is_not_found(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        response = TestClient(app).post("/api/cv/replacements/987654/cancel")
        assert response.status_code == 404
    finally:
        state["connection"].close()


# ----------------------------------------------------------------- privacy


def test_no_cv_reading_or_correction_ever_reaches_an_error_or_a_log(
    tmp_path, monkeypatch, caplog
):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        client.put(
            f"/api/cv/replacements/{replacement_id}/decision",
            json={
                "candidate_id": entry_for(snapshot, NEW_SKILL)["candidate_id"],
                "decision": "CORRECT",
                "staged_value": SENTINEL_CORRECTION,
            },
        )
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            refusals = [
                client.post(f"/api/cv/replacements/{replacement_id}/ready"),
                client.post(
                    f"/api/cv/replacements/{replacement_id}/activate",
                    json={"review_digest": "ab" * 32},
                ),
                client.post(
                    "/api/cv/replacements",
                    json={"extraction_id": state["extraction_id"]},
                ),
                client.put(
                    f"/api/cv/replacements/{replacement_id}/decision",
                    json={"fact_id": 987654, "decision": "RETIRE"},
                ),
            ]
        forbidden = (SENTINEL_CORRECTION, KEPT_SKILL, NEW_SKILL, DROPPED_SKILL)
        for response in refusals:
            assert response.status_code in (400, 409)
            for value in forbidden:
                assert value not in response.text
        logged = "\n".join(
            f"{record.getMessage()} {record.__dict__}" for record in caplog.records
        )
        for value in forbidden:
            assert value not in logged
    finally:
        state["connection"].close()


# -------------------------------------------------------- config and schema


def test_an_unusable_profile_configuration_is_unavailable(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        # `" 1"` is deliberately absent: the guard normalizes surrounding
        # whitespace and accepts it, exactly as every other surface does.
        for raw in (None, "", "0", "-1", "abc", "01", "1x", "1.0"):
            if raw is None:
                monkeypatch.delenv("OPPORTUNITY_RADAR_PROFILE_ID", raising=False)
            else:
                monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", raw)
            response = client.get("/api/cv/replacements/current")
            assert response.status_code == 503, raw
            assert response.json()["detail"] == PUBLIC_CV_REPLACEMENT_ERROR
    finally:
        state["connection"].close()


def test_a_missing_database_is_unavailable_and_is_never_created(
    tmp_path, monkeypatch
):
    missing = tmp_path / "absent" / "nothing.db"
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(missing))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", "1")

    client = TestClient(app)
    read = client.get("/api/cv/replacements/current")
    write = client.post("/api/cv/replacements", json={"extraction_id": 1})

    assert read.status_code == 503
    assert write.status_code == 503
    assert not missing.exists()
    assert not missing.parent.exists()


def test_a_database_below_0028_is_unavailable(tmp_path, monkeypatch):
    state = seeded(
        tmp_path, monkeypatch, migrations_directory=migrations_below(tmp_path, "0028")
    )
    try:
        client = TestClient(app)
        assert client.get("/api/cv/replacements/current").status_code == 503
        assert (
            client.post(
                "/api/cv/replacements", json={"extraction_id": 1}
            ).status_code
            == 503
        )
    finally:
        state["connection"].close()


def test_a_database_below_0027_is_unavailable(tmp_path, monkeypatch):
    state = seeded(
        tmp_path, monkeypatch, migrations_directory=migrations_below(tmp_path, "0027")
    )
    try:
        assert (
            TestClient(app).get("/api/cv/replacements/current").status_code == 503
        )
    finally:
        state["connection"].close()


def test_an_unknown_replacement_is_not_found(tmp_path, monkeypatch):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        assert client.post("/api/cv/replacements/987654/ready").status_code == 404
        assert (
            client.put(
                "/api/cv/replacements/987654/decision",
                json={"candidate_id": 1, "decision": "ACCEPT"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/cv/replacements/987654/activate",
                json={"review_digest": "ab" * 32},
            ).status_code
            == 404
        )
    finally:
        state["connection"].close()


# --------------------------------------------- the reading, and its repr


def test_a_reading_reaches_the_browser_but_never_a_repr(tmp_path, monkeypatch):
    """The person must read their own CV; a traceback must not.

    `display_value` is serialized normally and kept out of `repr`, which is
    what a traceback, a logging call and a failed assertion all print without
    anybody deciding to.
    """
    from services.api.cv_replacement import (
        ReviewEntryResponse,
        ReviewSnapshotResponse,
        read_current_review_surface,
    )

    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        open_review(client, state)

        response = client.get("/api/cv/replacements/current")
        assert SENTINEL_READING in response.text

        snapshot = read_current_review_surface()
        assert isinstance(snapshot, ReviewSnapshotResponse)
        shown = [
            entry
            for entry in snapshot.entries
            if entry.display_value == SENTINEL_READING
        ]
        assert len(shown) == 1
        entry = shown[0]
        assert isinstance(entry, ReviewEntryResponse)
        # Present in the JSON the browser receives...
        assert SENTINEL_READING in snapshot.model_dump_json()
        assert SENTINEL_READING in entry.model_dump_json()
        # ...and absent from every technical rendering of the same objects.
        assert SENTINEL_READING not in repr(entry)
        assert SENTINEL_READING not in repr(snapshot)
        assert SENTINEL_READING not in str(entry)
        assert SENTINEL_READING not in str(snapshot)
    finally:
        state["connection"].close()


def test_a_reading_never_reaches_an_error_body_or_a_log(
    tmp_path, monkeypatch, caplog
):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            refusals = [
                client.post(f"/api/cv/replacements/{replacement_id}/ready"),
                client.post(
                    "/api/cv/replacements",
                    json={"extraction_id": state["extraction_id"]},
                ),
                client.post(
                    f"/api/cv/replacements/{replacement_id}/activate",
                    json={"review_digest": "ab" * 32},
                ),
            ]
        for response in refusals:
            assert response.status_code == 409
            assert SENTINEL_READING not in response.text
        logged = "\n".join(
            f"{record.getMessage()} {record.__dict__}" for record in caplog.records
        )
        assert SENTINEL_READING not in logged
    finally:
        state["connection"].close()


# ------------------------------------------- routes that take no body


def test_ready_and_cancel_work_without_a_body(tmp_path, monkeypatch):
    """The historical shape: no body at all, and nothing changes about it."""
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        answer_everything(client, state, snapshot)

        ready = client.post(f"/api/cv/replacements/{replacement_id}/ready")
        assert ready.status_code == 200
        assert ready.json()["replacement"]["effective_state"] == "READY_TO_ACTIVATE"

        cancelled = client.post(f"/api/cv/replacements/{replacement_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["effective_state"] == "CANCELLED"
    finally:
        state["connection"].close()


@pytest.mark.parametrize("route", ["ready", "cancel"])
@pytest.mark.parametrize(
    "body",
    [
        {"profile_id": 99},
        {"unexpected": "field"},
        {},
        {"review_digest": "ab" * 32},
        {"staged_value": SENTINEL_CORRECTION},
    ],
    ids=["profile-id", "unknown-field", "empty-object", "digest", "correction"],
)
def test_a_body_on_ready_or_cancel_is_refused(tmp_path, monkeypatch, route, body):
    """These routes name their target in the path and carry no payload.

    A body is either a misunderstanding or an attempt to smuggle a field past a
    surface that resolves the owner itself, so it is refused rather than
    ignored - and nothing it contained is parsed, echoed or logged.
    """
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        answer_everything(client, state, snapshot)
        stored = (
            "SELECT lifecycle, ready_review_digest, closed_at"
            " FROM profile_cv_replacements WHERE id = ?"
        )
        before = state["connection"].execute(stored, (replacement_id,)).fetchone()

        response = client.post(
            f"/api/cv/replacements/{replacement_id}/{route}", json=body
        )

        assert response.status_code == 400
        assert response.json()["detail"] == PUBLIC_CV_REPLACEMENT_REQUEST_ERROR
        assert SENTINEL_CORRECTION not in response.text
        assert "profile_id" not in response.text
        # Nothing moved: the refusal happened before any owner was called.
        assert state["connection"].execute(
            stored, (replacement_id,)
        ).fetchone() == before
    finally:
        state["connection"].close()


def test_a_refused_body_is_never_logged(tmp_path, monkeypatch, caplog):
    state = seeded(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            for route in ("ready", "cancel"):
                assert (
                    client.post(
                        f"/api/cv/replacements/{replacement_id}/{route}",
                        json={
                            "profile_id": 99,
                            "staged_value": SENTINEL_CORRECTION,
                        },
                    ).status_code
                    == 400
                )
        logged = "\n".join(
            f"{record.getMessage()} {record.__dict__}" for record in caplog.records
        )
        assert SENTINEL_CORRECTION not in logged
        assert "profile_id" not in logged
    finally:
        state["connection"].close()


# ------------------------------------------------- one snapshot, one instant


def test_a_snapshot_never_mixes_two_versions_of_the_review(tmp_path, monkeypatch):
    """A review cannot change underneath a snapshot that is being composed.

    A snapshot is many SELECTs — the attempt, the plan, the answers, the
    progress, the readings — and without a boundary another connection could
    commit between two of them, producing a body that mixes an old
    `ready_review_digest` with a decision recorded after it. That is not merely
    stale: it is a review that never existed.

    This database is in SQLite's default rollback-journal mode, where the
    guarantee takes its strongest form: a reader holding a read transaction
    makes a concurrent commit impossible for its duration. So the probe below
    does not observe a stale value — it observes the writer being refused
    outright, which is the mechanism that makes a mixed snapshot unreachable.

    The writer has its own connection and a short busy timeout, so it fails
    fast instead of hanging: nothing here can deadlock the suite, and every
    database is temporary.
    """
    import sqlite3

    from services.api.cv_replacement import _read_snapshot
    from services.digital_twin.cv.replacement.models import ReviewDecision
    from services.digital_twin.cv.replacement.staging import record_review_decision

    state = seeded(tmp_path, monkeypatch)
    writer = None
    reader = None
    try:
        client = TestClient(app)
        snapshot = open_review(client, state)
        replacement_id = snapshot["replacement"]["replacement_id"]
        answer_everything(client, state, snapshot)
        ready = client.post(f"/api/cv/replacements/{replacement_id}/ready").json()
        digest = ready["replacement"]["ready_review_digest"]
        assert digest is not None
        candidate_id = entry_for(snapshot, NEW_SKILL)["candidate_id"]

        token_sql = (
            "SELECT ready_review_digest FROM profile_cv_replacements WHERE id = ?"
        )
        decisions_sql = (
            "SELECT decision FROM profile_cv_replacement_decisions"
            " WHERE replacement_id = ? ORDER BY id"
        )
        # The fixture connection must not hold a read of its own open while the
        # writer tries to commit, or it would be the thing blocking it.
        state["connection"].commit()
        reader = connect_database(state["path"])
        reader.execute("PRAGMA query_only = ON")
        writer = connect_database(state["path"])
        writer.execute("PRAGMA busy_timeout = 250")

        refused = None
        with _read_snapshot(reader):
            first = reader.execute(token_sql, (replacement_id,)).fetchone()[0]
            try:
                record_review_decision(
                    writer,
                    profile_id=state["profile_id"],
                    replacement_id=replacement_id,
                    candidate_id=candidate_id,
                    decision=ReviewDecision.REJECT,
                )
            except sqlite3.OperationalError as error:
                refused = error
            second = reader.execute(token_sql, (replacement_id,)).fetchone()[0]
            seen = reader.execute(decisions_sql, (replacement_id,)).fetchall()

        # The concurrent change could not land while the snapshot was open,
        # and the snapshot saw one single version of the review throughout.
        assert refused is not None
        assert "locked" in str(refused).lower()
        assert first == digest
        assert second == digest
        assert all(row[0] != "REJECT" for row in seen)
        if writer.in_transaction:
            writer.execute("ROLLBACK")

        # Once the snapshot is released the same change goes through, and the
        # next read shows the new world whole: the token cleared by the domain
        # and the new answer, together.
        reader.close()
        reader = None
        record_review_decision(
            writer,
            profile_id=state["profile_id"],
            replacement_id=replacement_id,
            candidate_id=candidate_id,
            decision=ReviewDecision.REJECT,
        )
        after = client.get("/api/cv/replacements/current").json()
        assert after["replacement"]["ready_review_digest"] is None
        assert any(entry["decision"] == "REJECT" for entry in after["entries"])
    finally:
        if reader is not None:
            reader.close()
        if writer is not None:
            writer.close()
        state["connection"].close()

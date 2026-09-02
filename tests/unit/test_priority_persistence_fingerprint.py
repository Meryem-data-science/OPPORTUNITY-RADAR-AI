from datetime import date

import pytest

from services.priority import canonical_priority_run_payload, priority_run_fingerprint


def values(**changes):
    result = {
        "input_assembly_version": "priority-input-assembly-v1",
        "profile_id": 7,
        "user_id": 3,
        "evaluation_date": date(2026, 9, 2),
        "matching_run_fingerprint": "a" * 64,
        "priority_engine_version": "priority-engine-v1",
        "priority_rules_version": "priority-rules-v1",
        "freshness_version": "priority-freshness-v1",
        "quality_version": "priority-quality-v1",
        "assessments": ((2, "b" * 64), (1, "c" * 64)),
    }
    result.update(changes)
    return result


def test_payload_is_canonical_and_excludes_technical_ids_and_timestamps():
    payload = canonical_priority_run_payload(**values())
    assert payload["assessments"] == [
        {"opportunity_id": 1, "assessment_fingerprint": "c" * 64},
        {"opportunity_id": 2, "assessment_fingerprint": "b" * 64},
    ]
    assert not ({"matching_run_id", "id", "created_at", "updated_at"} & payload.keys())


def test_order_does_not_change_fingerprint():
    assert priority_run_fingerprint(**values()) == priority_run_fingerprint(
        **values(assessments=tuple(reversed(values()["assessments"])))
    )


def test_matching_run_id_is_not_part_of_the_canonical_identity():
    payload = canonical_priority_run_payload(**values())
    assert "matching_run_id" not in payload
    with pytest.raises(TypeError, match="matching_run_id"):
        canonical_priority_run_payload(**values(), matching_run_id=999)


def test_each_operational_identity_field_changes_fingerprint():
    baseline = priority_run_fingerprint(**values())
    changes = (
        {"profile_id": 8},
        {"user_id": 4},
        {"evaluation_date": date(2026, 9, 3)},
        {"matching_run_fingerprint": "d" * 64},
        {"assessments": ((1, "c" * 64), (9, "b" * 64))},
        {"assessments": ((1, "d" * 64), (2, "b" * 64))},
        {"persistence_version": "priority-persistence-v2"},
        {"input_assembly_version": "priority-input-assembly-v2"},
        {"priority_engine_version": "priority-engine-v2"},
        {"priority_rules_version": "priority-rules-v2"},
        {"freshness_version": "priority-freshness-v2"},
        {"quality_version": "priority-quality-v2"},
    )
    assert all(
        priority_run_fingerprint(**values(**change)) != baseline for change in changes
    )

"""Unit coverage for the local Phase 3.2B candidate extraction CLI.

TEST ONLY content throughout: the CV below is invented and describes nobody.
The point of most of these tests is that this content never reaches stdout.
"""

import json
import os
import stat

import pytest

from services.digital_twin.cv.candidates import cli

# TEST ONLY. Every token here is checked to be absent from the default output.
PERSONAL_TEST_CONTENT = (
    "Jeanne Exemple",
    "jeanne@example.invalid",
    "+33 6 00 00 00 00",
    "github.com/exemple-compte",
    "Master fictif",
    "Stage analyste donnees",
    "Python",
)
FIRST_PAGE = [
    "Jeanne Exemple",
    "Data Scientist",
    "jeanne@example.invalid",
    "+33 6 00 00 00 00",
    "github.com/exemple-compte",
    "FORMATION",
    "Master fictif - Universite Exemple",
]
SECOND_PAGE = [
    "EXPERIENCE",
    "Stage analyste donnees",
    "COMPETENCES",
    "Python, SQL",
]


@pytest.fixture()
def cv_path(tmp_path, synthetic_pdf):
    path = tmp_path / "cv.pdf"
    path.write_bytes(synthetic_pdf([FIRST_PAGE, SECOND_PAGE]))
    return path


def test_the_default_output_reports_the_shape_and_no_cv_text(
    cv_path, capsys
) -> None:
    exit_code = cli.main([cli.EXTRACT_COMMAND, str(cv_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "extractor_version=cv-candidates-v3" in output
    assert "parser_version=cv-parser-v3" in output
    assert "NAME_CANDIDATE=1" in output
    assert "EMAIL=1" in output
    assert "GITHUB_URL=1" in output
    assert cli.UNVERIFIED_NOTICE in output
    for secret in PERSONAL_TEST_CONTENT:
        assert secret not in output


def test_the_cv_path_is_never_echoed_back(cv_path, capsys) -> None:
    cli.main([cli.EXTRACT_COMMAND, str(cv_path)])

    assert str(cv_path) not in capsys.readouterr().out


def test_a_failure_reports_the_error_without_the_path(tmp_path, capsys) -> None:
    missing = tmp_path / "absent.pdf"

    exit_code = cli.main([cli.EXTRACT_COMMAND, str(missing)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "PdfFileNotFoundError" in output
    assert str(missing) not in output


def test_nothing_is_written_without_the_export_flag(cv_path, tmp_path) -> None:
    before = set(tmp_path.iterdir())

    cli.main([cli.EXTRACT_COMMAND, str(cv_path)])

    assert set(tmp_path.iterdir()) == before


def test_the_export_writes_the_candidates_and_their_provenance(
    cv_path, tmp_path, capsys
) -> None:
    destination = tmp_path / "candidates.json"

    exit_code = cli.main(
        [cli.EXTRACT_COMMAND, str(cv_path), "--json-out", str(destination)]
    )

    assert exit_code == 0
    assert cli.JSON_OUT_NOTICE in capsys.readouterr().out
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["extractor_version"] == "cv-candidates-v3"
    emails = [
        candidate
        for candidate in payload["candidates"]
        if candidate["candidate_type"] == "EMAIL"
    ]
    assert [candidate["raw_text"] for candidate in emails] == [
        "jeanne@example.invalid"
    ]
    assert emails[0]["page_numbers"] == [1]
    assert emails[0]["rule_id"] == "EMAIL_PATTERN"
    assert emails[0]["cv_sha256"] == payload["sha256"]


def test_an_existing_export_path_is_never_overwritten(
    cv_path, tmp_path, capsys
) -> None:
    destination = tmp_path / "candidates.json"
    destination.write_text("keep me", encoding="utf-8")

    exit_code = cli.main(
        [cli.EXTRACT_COMMAND, str(cv_path), "--json-out", str(destination)]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert cli.EXISTING_JSON_OUT_ERROR in output
    assert str(destination) not in output
    assert destination.read_text(encoding="utf-8") == "keep me"


def test_the_export_is_readable_by_its_owner_alone(cv_path, tmp_path) -> None:
    destination = tmp_path / "candidates.json"

    cli.main([cli.EXTRACT_COMMAND, str(cv_path), "--json-out", str(destination)])

    mode = stat.S_IMODE(os.stat(destination).st_mode)
    assert mode == cli.DETAILED_FILE_MODE == 0o600

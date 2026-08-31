"""Unit coverage for the local CV parser CLI.

TEST ONLY content throughout: the CV below is invented and describes nobody.
The point of most of these tests is that this content never reaches stdout.
"""

import hashlib
import json
import os
import stat

import pytest

from services.digital_twin.cv import cli

# TEST ONLY. Every token here is checked to be absent from the default output.
PERSONAL_TEST_CONTENT = (
    "Jeanne Exemple",
    "jeanne@example.invalid",
    "+33 6 00 00 00 00",
    "12 rue Exemple, Paris",
    "Etudiante en donnees et IA.",
    "Stage analyste donnees",
    "Python, SQL",
)
FIRST_PAGE = [
    "Jeanne Exemple",
    "jeanne@example.invalid",
    "+33 6 00 00 00 00",
    "12 rue Exemple, Paris",
    "PROFIL",
    "Etudiante en donnees et IA.",
]
SECOND_PAGE = [
    "EXPERIENCE",
    "Stage analyste donnees",
    "COMPETENCES",
    "Python, SQL",
]


def _cv(tmp_path, synthetic_pdf, name: str = "cv.pdf"):
    path = tmp_path / name
    path.write_bytes(synthetic_pdf([FIRST_PAGE, SECOND_PAGE]))
    return path


def test_parse_reports_the_shape_of_the_document(
    tmp_path, synthetic_pdf, capsys
) -> None:
    path = _cv(tmp_path, synthetic_pdf)

    exit_code = cli.main(["parse", str(path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "parser_version=cv-parser-v5" in output
    assert "pages=2" in output
    assert "sections=UNCLASSIFIED,PROFILE,EXPERIENCE,SKILLS" in output
    assert "UNCLASSIFIED_LEADING_CONTENT" in output


def test_the_default_output_never_leaks_personal_content(
    tmp_path, synthetic_pdf, capsys
) -> None:
    path = _cv(tmp_path, synthetic_pdf)

    assert cli.main(["parse", str(path)]) == 0

    # The whole stream, structured log line included, is checked.
    output = capsys.readouterr().out
    for secret in PERSONAL_TEST_CONTENT:
        assert secret not in output


def test_a_failure_reports_the_error_without_quoting_the_path(tmp_path, capsys) -> None:
    marker = "Jeanne-Exemple-TEST-ONLY"
    path = tmp_path / f"CV-{marker}.pdf"
    path.write_bytes(b"not a pdf at all")

    exit_code = cli.main(["parse", str(path)])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "cv parse failed: InvalidPdfError" in output
    assert marker not in output


def test_a_missing_file_fails_without_a_traceback(tmp_path, capsys) -> None:
    exit_code = cli.main(["parse", str(tmp_path / "absent.pdf")])

    assert exit_code == 1
    assert "PdfFileNotFoundError" in capsys.readouterr().out


def test_json_out_writes_the_detailed_result_only_where_it_is_asked_to(
    tmp_path, synthetic_pdf, capsys
) -> None:
    path = _cv(tmp_path, synthetic_pdf)
    destination = tmp_path / "detailed.json"

    exit_code = cli.main(["parse", str(path), "--json-out", str(destination)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert cli.JSON_OUT_NOTICE in output
    detailed = json.loads(destination.read_text(encoding="utf-8"))
    assert detailed["parser_version"] == "cv-parser-v5"
    assert "Stage analyste donnees" in detailed["pages"][1]["text"]
    # The requested file holds the text; the terminal still does not.
    for secret in PERSONAL_TEST_CONTENT:
        assert secret not in output


def test_json_out_never_overwrites_an_existing_file(
    tmp_path, synthetic_pdf, capsys
) -> None:
    path = _cv(tmp_path, synthetic_pdf)
    marker = "detailed-TEST-ONLY"
    destination = tmp_path / f"{marker}.json"
    original = b"keep me byte for byte \x00\xff"
    destination.write_bytes(original)

    exit_code = cli.main(["parse", str(path), "--json-out", str(destination)])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "FileExistsError" in output
    assert cli.EXISTING_JSON_OUT_ERROR in output
    # Exclusive creation: the existing file is not truncated, not appended to,
    # and not touched at all.
    assert destination.read_bytes() == original
    # The refusal names the rule, never the path the operator gave.
    assert marker not in output


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_the_detailed_export_is_readable_by_its_owner_only(
    tmp_path, synthetic_pdf, capsys
) -> None:
    path = _cv(tmp_path, synthetic_pdf)
    destination = tmp_path / "detailed.json"

    assert cli.main(["parse", str(path), "--json-out", str(destination)]) == 0
    capsys.readouterr()

    mode = stat.S_IMODE(destination.stat().st_mode)
    assert mode == cli.DETAILED_FILE_MODE == 0o600
    # No group and no other bit, whatever the umask of the shell that ran it.
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def test_no_file_is_written_when_json_out_is_not_given(
    tmp_path, synthetic_pdf, capsys
) -> None:
    path = _cv(tmp_path, synthetic_pdf)
    before = sorted(item.name for item in tmp_path.iterdir())

    assert cli.main(["parse", str(path)]) == 0
    capsys.readouterr()

    assert sorted(item.name for item in tmp_path.iterdir()) == before


def test_the_reported_sha256_identifies_the_exact_file(
    tmp_path, synthetic_pdf, capsys
) -> None:
    path = _cv(tmp_path, synthetic_pdf)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()

    assert cli.main(["parse", str(path)]) == 0

    assert f"sha256={expected}" in capsys.readouterr().out

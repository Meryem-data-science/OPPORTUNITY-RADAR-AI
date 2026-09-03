"""Unit coverage for the local VAPID bootstrap command.

Every key in this module is generated into a temporary directory and thrown
away with it. Nothing here writes to the repository, nothing here is a real
deployment identity, and the one thing every test watches for is the private
key appearing anywhere it should not: stdout, stderr, a log record, a repr, or
a traceback.
"""

import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from services.notifications import vapid_bootstrap
from services.notifications.vapid_bootstrap import (
    DEFAULT_OUTPUT_PATH,
    SECRET_FILE_MODE,
    VapidBootstrapError,
    generate_vapid_identity,
    render_identity_file,
    write_vapid_identity,
)
from services.notifications.web_push import (
    PRIVATE_KEY_BYTE_LENGTH,
    VAPID_PRIVATE_KEY_VARIABLE,
    VAPID_PUBLIC_KEY_VARIABLE,
    VAPID_SUBJECT_VARIABLE,
    WebPushConfigurationError,
    build_vapid_configuration,
    decode_base64url,
    load_vapid_configuration,
)

SUBJECT = "mailto:ops@example.invalid"


def read_identity(path):
    """Parse the written file the way an operator's shell would."""
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


def test_a_generated_identity_is_a_real_pair_the_sender_accepts():
    identity = generate_vapid_identity(SUBJECT)

    private = decode_base64url(identity._private_key)
    public = decode_base64url(identity.public_key)
    assert private is not None and len(private) == PRIVATE_KEY_BYTE_LENGTH
    assert public is not None and len(public) == 65 and public[0] == 0x04
    # The whole point of the encodings: the sender's own loader takes them, and
    # it is the loader that checks the public half really derives from the
    # private one rather than merely being the right shape.
    configuration = build_vapid_configuration(
        public_key=identity.public_key,
        private_key=identity._private_key,
        subject=identity.subject,
    )
    assert configuration.public_key == identity.public_key
    assert configuration.subject == SUBJECT


def test_two_runs_never_produce_the_same_identity():
    first = generate_vapid_identity(SUBJECT)
    second = generate_vapid_identity(SUBJECT)

    assert first.public_key != second.public_key
    assert first._private_key != second._private_key


def test_a_public_key_from_one_pair_never_validates_against_another():
    """The pairing check is real, not a formality."""
    first = generate_vapid_identity(SUBJECT)
    second = generate_vapid_identity(SUBJECT)

    with pytest.raises(WebPushConfigurationError, match="do not form a pair"):
        build_vapid_configuration(
            public_key=first.public_key,
            private_key=second._private_key,
            subject=SUBJECT,
        )


@pytest.mark.parametrize(
    "subject",
    ["", "   ", "ops@example.invalid", "http://example.invalid", "mailto:", "tel:123"],
)
def test_a_subject_a_push_service_would_refuse_is_refused_before_any_key_exists(
    subject,
):
    with pytest.raises(VapidBootstrapError, match=VAPID_SUBJECT_VARIABLE):
        generate_vapid_identity(subject)


@pytest.mark.parametrize("subject", [SUBJECT, "https://ops.example.invalid/contact"])
def test_both_subject_forms_rfc_8292_allows_are_accepted(subject):
    assert generate_vapid_identity(subject).subject == subject


def test_the_written_file_is_exactly_what_the_sender_reads_back(tmp_path):
    identity = generate_vapid_identity(SUBJECT)
    path = tmp_path / "secrets" / "web-push.env"

    written = write_vapid_identity(path, identity)

    assert written == path and path.is_file()
    values = read_identity(path)
    assert set(values) == {
        VAPID_PUBLIC_KEY_VARIABLE,
        VAPID_PRIVATE_KEY_VARIABLE,
        VAPID_SUBJECT_VARIABLE,
    }
    # The file is the environment: loading it is what a deployment does.
    configuration = load_vapid_configuration(values)
    assert configuration.public_key == identity.public_key
    assert configuration.subject == SUBJECT


def test_an_existing_identity_is_never_replaced_unless_it_is_asked_for(tmp_path):
    """Replacing the public key invalidates every subscription made with it."""
    path = tmp_path / "web-push.env"
    first = generate_vapid_identity(SUBJECT)
    write_vapid_identity(path, first)
    original = path.read_text(encoding="utf-8")

    with pytest.raises(VapidBootstrapError, match="refusing to replace"):
        write_vapid_identity(path, generate_vapid_identity(SUBJECT))
    assert path.read_text(encoding="utf-8") == original

    second = generate_vapid_identity(SUBJECT)
    write_vapid_identity(path, second, overwrite=True)
    assert read_identity(path)[VAPID_PUBLIC_KEY_VARIABLE] == second.public_key


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_the_secret_file_is_readable_by_its_owner_alone(tmp_path):
    path = tmp_path / "web-push.env"
    write_vapid_identity(path, generate_vapid_identity(SUBJECT))

    assert stat.S_IMODE(path.stat().st_mode) == SECRET_FILE_MODE
    assert vapid_bootstrap.file_is_owner_only(path)


def test_the_private_key_never_renders_itself_anywhere(tmp_path, caplog):
    identity = generate_vapid_identity(SUBJECT)
    secret = identity._private_key

    with caplog.at_level(logging.DEBUG):
        assert secret not in repr(identity)
        assert secret not in str(identity)
        assert secret not in f"{identity}"
        # A pytest diff, a traceback frame, and a log record all go through one
        # of the three above; none of them may carry the scalar.
        try:
            write_vapid_identity(tmp_path, identity)
        except VapidBootstrapError as error:
            assert secret not in str(error)
    assert secret not in caplog.text


def test_the_command_writes_the_secret_and_prints_only_the_public_half(
    tmp_path, capsys, caplog
):
    path = tmp_path / "web-push.env"

    with caplog.at_level(logging.DEBUG):
        assert vapid_bootstrap.main(["--subject", SUBJECT, "--output", str(path)]) == 0

    captured = capsys.readouterr()
    values = read_identity(path)
    secret = values[VAPID_PRIVATE_KEY_VARIABLE]
    assert (
        f"{VAPID_PUBLIC_KEY_VARIABLE}={values[VAPID_PUBLIC_KEY_VARIABLE]}"
        in captured.out
    )
    assert str(path) in captured.out
    assert captured.err == ""
    # The one value the operator must never see echoed.
    assert secret not in captured.out
    assert secret not in captured.err
    assert secret not in caplog.text
    assert VAPID_PRIVATE_KEY_VARIABLE not in captured.out


def test_the_command_refuses_an_existing_file_and_names_no_key(tmp_path, capsys):
    path = tmp_path / "web-push.env"
    assert vapid_bootstrap.main(["--subject", SUBJECT, "--output", str(path)]) == 0
    original = path.read_text(encoding="utf-8")
    capsys.readouterr()

    assert vapid_bootstrap.main(["--subject", SUBJECT, "--output", str(path)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refusing to replace" in captured.err
    assert path.read_text(encoding="utf-8") == original
    secret = read_identity(path)[VAPID_PRIVATE_KEY_VARIABLE]
    assert secret not in captured.err


def test_the_command_refuses_a_bad_subject_and_writes_nothing(tmp_path, capsys):
    path = tmp_path / "web-push.env"

    assert vapid_bootstrap.main(["--subject", "nonsense", "--output", str(path)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert VAPID_SUBJECT_VARIABLE in captured.err
    assert not path.exists()


def test_the_default_path_is_inside_the_git_ignored_secrets_directory():
    """Nothing this command writes can be committed by accident."""
    assert DEFAULT_OUTPUT_PATH.parts[0] == ".secrets"
    ignored = [
        line.strip()
        for line in Path(".gitignore").read_text(encoding="utf-8").splitlines()
    ]
    assert ".secrets/" in ignored


def test_nothing_generates_an_identity_on_its_own(tmp_path, monkeypatch):
    """Importing the application never mints a key, and never writes one.

    A key that appears by itself is a key nobody decided on, and the public
    half of it is already in every browser subscription by the time anyone
    notices. So generation only ever happens because a person ran the command.
    """

    def forbidden(*arguments, **keywords):  # pragma: no cover - must not run
        raise AssertionError("a VAPID identity is only ever generated on request")

    monkeypatch.setattr(vapid_bootstrap, "generate_vapid_identity", forbidden)
    monkeypatch.setattr(vapid_bootstrap, "write_vapid_identity", forbidden)
    monkeypatch.chdir(tmp_path)
    for variable in (
        VAPID_PUBLIC_KEY_VARIABLE,
        VAPID_PRIVATE_KEY_VARIABLE,
        VAPID_SUBJECT_VARIABLE,
    ):
        monkeypatch.delenv(variable, raising=False)

    import importlib

    for module in (
        "services.notifications",
        "services.api.push",
        "services.notifications.delivery_cli",
    ):
        importlib.reload(importlib.import_module(module))

    assert not (tmp_path / ".secrets").exists()
    assert os.listdir(tmp_path) == []


def test_the_rendered_file_documents_itself_without_quoting_a_key():
    identity = generate_vapid_identity(SUBJECT)

    rendered = render_identity_file(identity)

    comments = [line for line in rendered.splitlines() if line.startswith("#")]
    assert comments
    assert identity._private_key not in "\n".join(comments)
    assert rendered.endswith("\n")

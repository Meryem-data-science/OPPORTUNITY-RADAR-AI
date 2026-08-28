"""Unit coverage for deterministic Digital Twin email normalization."""

import pytest

from services.digital_twin.identity import InvalidEmailError, normalize_email

# TEST ONLY addresses. `.invalid` can never resolve, and no real address is
# ever hardcoded in this repository.
TEST_ONLY_EMAIL = "student@example.invalid"


@pytest.mark.parametrize(
    "value",
    [
        TEST_ONLY_EMAIL,
        "Student@Example.invalid",
        "STUDENT@EXAMPLE.INVALID",
        "  student@example.invalid  ",
        "\tStudent@Example.invalid\n",
    ],
)
def test_case_and_surrounding_space_share_one_canonical_form(value: str) -> None:
    assert normalize_email(value) == TEST_ONLY_EMAIL


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "no-at-sign.invalid",
        "two@at@example.invalid",
        "@example.invalid",
        "student@",
        "student@localhost",
        "student@example..invalid",
        "student@.invalid",
        "student@-example.invalid",
        "student@example-.invalid",
        "stu dent@example.invalid",
        "student@exa mple.invalid",
        "stu<dent@example.invalid",
        "student@exam;ple.invalid",
        "a" * 65 + "@example.invalid",
        "a" * 250 + "@example.invalid",
        None,
        42,
    ],
)
def test_manifestly_invalid_addresses_are_refused(value: object) -> None:
    with pytest.raises(InvalidEmailError):
        normalize_email(value)


def test_refusal_never_quotes_the_rejected_address() -> None:
    secret = "sensitive.local.part@example.invalid extra"

    with pytest.raises(InvalidEmailError) as failure:
        normalize_email(secret)

    assert "sensitive.local.part" not in str(failure.value)

"""Transactional persistence for the user and profile root.

The project talks to SQLite directly and this layer keeps doing so: no ORM, no
new connection factory, no second migration runner. It owns exactly two
operations — create-or-return the pair, and read it back — and it never invents
a profile for a user that does not exist.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from services.digital_twin.identity import normalize_email
from services.digital_twin.models import Profile, User, UserProfile


class UserProfileError(RuntimeError):
    """Raised when the user/profile root cannot be read or written safely."""


def _user_from_row(row: tuple) -> User:
    return User(id=int(row[0]), email=str(row[1]), created_at=str(row[2]),
                updated_at=str(row[3]))


def _profile_from_row(row: tuple) -> Profile:
    return Profile(id=int(row[0]), user_id=int(row[1]), created_at=str(row[2]),
                   updated_at=str(row[3]))


def _select_user(connection: sqlite3.Connection, email: str) -> User | None:
    row = connection.execute(
        "SELECT id, email, created_at, updated_at FROM users WHERE email = ?",
        (email,),
    ).fetchone()
    return None if row is None else _user_from_row(row)


def _select_profile(connection: sqlite3.Connection, user_id: int) -> Profile | None:
    row = connection.execute(
        "SELECT id, user_id, created_at, updated_at FROM profiles WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return None if row is None else _profile_from_row(row)


def ensure_user_profile(
    connection: sqlite3.Connection,
    email: str,
    *,
    after_user: Callable[[], None] | None = None,
) -> UserProfile:
    """Create or return the one user and the one profile for this address.

    The whole operation runs in a single explicit transaction: a first call
    creates both rows, any later call with the same address — whatever its case
    or surrounding spaces — returns the same ids and creates nothing. A failure
    at any point rolls the transaction back, so a user is never left without
    the profile it owns.

    `after_user` is a test seam invoked once the user row exists and before the
    profile is created; production callers leave it unset.
    """
    normalized = normalize_email(email)
    connection.execute("BEGIN IMMEDIATE")
    try:
        user = _select_user(connection, normalized)
        created = user is None
        if user is None:
            row = connection.execute(
                """INSERT INTO users (email) VALUES (?)
                   RETURNING id, email, created_at, updated_at""",
                (normalized,),
            ).fetchone()
            if row is None:
                raise UserProfileError("user insert returned no row")
            user = _user_from_row(row)
        if after_user is not None:
            after_user()
        profile = _select_profile(connection, user.id)
        if profile is None:
            row = connection.execute(
                """INSERT INTO profiles (user_id) VALUES (?)
                   RETURNING id, user_id, created_at, updated_at""",
                (user.id,),
            ).fetchone()
            if row is None:
                raise UserProfileError("profile insert returned no row")
            profile = _profile_from_row(row)
        else:
            created = False
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return UserProfile(user=user, profile=profile, created=created)


def get_user_profile_by_email(
    connection: sqlite3.Connection, email: str
) -> UserProfile | None:
    """Return the persisted pair for this address, or None when absent.

    A user without a profile is a broken invariant rather than an absence, so
    it is refused instead of being answered with an invented profile.
    """
    normalized = normalize_email(email)
    user = _select_user(connection, normalized)
    if user is None:
        return None
    profile = _select_profile(connection, user.id)
    if profile is None:
        raise UserProfileError("user exists without the profile it must own")
    return UserProfile(user=user, profile=profile)

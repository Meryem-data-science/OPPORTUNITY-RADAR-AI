"""Typed, framework-free representations of the persistent Digital Twin root."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    """The identity and ownership root. `email` is already normalized."""

    id: int
    email: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Profile:
    """The stable Digital Twin root owned by exactly one user.

    Phase 3.1A gives it no factual column on purpose: facts and their
    provenance arrive with `profile_facts` in a later slice.
    """

    id: int
    user_id: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class UserProfile:
    """One user together with the single profile it owns."""

    user: User
    profile: Profile
    #: True only when the call that produced this pair created it. A read of an
    #: existing pair always reports False.
    created: bool = False

    @property
    def user_id(self) -> int:
        return self.user.id

    @property
    def profile_id(self) -> int:
        return self.profile.id

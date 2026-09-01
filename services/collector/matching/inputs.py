"""Read-only assembly of matching inputs from Phase 3 projections."""

from __future__ import annotations

import sqlite3

from services.collector.extractors.opportunity_constraints.requirements.repository import (
    read_opportunity_requirements,
)
from services.digital_twin.preferences.models import (
    CareerObjectives,
    OpportunityPreferences,
)
from services.digital_twin.preferences.repository import (
    get_profile_career_objectives,
    get_profile_preferences,
)
from services.digital_twin.skills.repository import list_profile_skills
from services.digital_twin.structured_profile.repository import (
    list_profile_educations,
    list_profile_experiences,
    list_profile_projects,
)

from .models import (
    MatchingCareerObjectives,
    MatchingEducation,
    MatchingExperience,
    MatchingInput,
    MatchingOpportunityInput,
    MatchingPreferences,
    MatchingProfileInput,
    MatchingProfileSkill,
    MatchingProject,
    MatchingQualification,
    MatchingRequiredSkill,
    MatchingRequirementAmbiguity,
    MatchingRequirements,
)


class MatchingInputError(RuntimeError):
    """Raised when an identified matching input does not exist."""


def load_profile_matching_input(
    connection: sqlite3.Connection, profile_id: int
) -> MatchingProfileInput:
    """Build the profile side exclusively from persisted Phase 3 projections."""
    if (
        connection.execute(
            "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        is None
    ):
        raise MatchingInputError(f"profile {profile_id} does not exist")

    preference_row = get_profile_preferences(connection, profile_id)
    preferences = None
    if preference_row is not None:
        value = preference_row.value
        if not isinstance(value, OpportunityPreferences):
            raise MatchingInputError(
                "profile preferences projection has an invalid value"
            )
        preferences = MatchingPreferences(
            opportunity_types=tuple(item.value for item in value.opportunity_types),
            work_modes=tuple(item.value for item in value.work_modes),
            preferred_domains=value.preferred_domains,
            input_version=preference_row.input_version,
        )

    objective_row = get_profile_career_objectives(connection, profile_id)
    career_objectives = None
    if objective_row is not None:
        value = objective_row.value
        if not isinstance(value, CareerObjectives):
            raise MatchingInputError(
                "career objectives projection has an invalid value"
            )
        career_objectives = MatchingCareerObjectives(
            objectives=value.objectives, input_version=objective_row.input_version
        )

    return MatchingProfileInput(
        profile_id=profile_id,
        skills=tuple(
            MatchingProfileSkill(
                canonical_key=item.canonical_key,
                canonical_name=item.canonical_name,
                normalizer_versions=tuple(
                    sorted({evidence.normalizer_version for evidence in item.evidence})
                ),
            )
            for item in list_profile_skills(connection, profile_id)
        ),
        experiences=tuple(
            MatchingExperience(
                item.role_text, item.description_text, item.structurer_version
            )
            for item in list_profile_experiences(connection, profile_id)
        ),
        projects=tuple(
            MatchingProject(
                item.title_text, item.description_text, item.structurer_version
            )
            for item in list_profile_projects(connection, profile_id)
        ),
        educations=tuple(
            MatchingEducation(
                item.program_text, item.description_text, item.structurer_version
            )
            for item in list_profile_educations(connection, profile_id)
        ),
        preferences=preferences,
        career_objectives=career_objectives,
    )


def load_opportunity_matching_input(
    connection: sqlite3.Connection, opportunity_id: int
) -> MatchingOpportunityInput:
    """Build the opportunity side without qualification or requirement work."""
    row = connection.execute(
        """SELECT o.canonical_title, o.description, o.remote_type,
                  q.qualification, q.primary_domain, q.opportunity_type,
                  q.classifier_version
             FROM opportunities AS o
             LEFT JOIN opportunity_qualifications AS q ON q.opportunity_id = o.id
            WHERE o.id = ?""",
        (opportunity_id,),
    ).fetchone()
    if row is None:
        raise MatchingInputError(f"opportunity {opportunity_id} does not exist")

    qualification = None
    if row[3] is not None:
        qualification = MatchingQualification(
            qualification=str(row[3]),
            primary_domain=str(row[4]),
            opportunity_type=str(row[5]),
            classifier_version=str(row[6]),
        )

    extracted = read_opportunity_requirements(connection, opportunity_id)
    requirements = None
    if extracted is not None:
        requirements = MatchingRequirements(
            extractor_version=extracted.extractor_version,
            skills=tuple(
                MatchingRequiredSkill(
                    canonical_key=item.canonical_key,
                    canonical_name=item.canonical_name,
                    requirement=item.requirement.value,
                )
                for item in extracted.skills
            ),
            ambiguities=tuple(
                MatchingRequirementAmbiguity(
                    kind=item.kind.value, reason=item.reason.value
                )
                for item in extracted.ambiguities
            ),
        )

    return MatchingOpportunityInput(
        opportunity_id=opportunity_id,
        canonical_title=str(row[0]),
        description=None if row[1] is None else str(row[1]),
        remote_type=None if row[2] is None else str(row[2]),
        qualification=qualification,
        requirements=requirements,
    )


def load_matching_input(
    connection: sqlite3.Connection, profile_id: int, opportunity_id: int
) -> MatchingInput:
    """Build both read-only input sides for one identified pair."""
    return MatchingInput(
        profile=load_profile_matching_input(connection, profile_id),
        opportunity=load_opportunity_matching_input(connection, opportunity_id),
    )

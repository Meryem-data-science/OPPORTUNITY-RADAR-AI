"""Pure parsers for transient collector inputs."""

from services.collector.parsers.linkedin_job_alert import (
    LINKEDIN_JOB_ALERT_SOURCE_ID,
    parse_linkedin_job_alert,
)

__all__ = ["LINKEDIN_JOB_ALERT_SOURCE_ID", "parse_linkedin_job_alert"]

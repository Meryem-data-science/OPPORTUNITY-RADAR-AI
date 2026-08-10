"""Generic, read-only Gmail intake support."""

from services.collector.gmail.client import (
    GMAIL_READONLY_SCOPE,
    GmailApiError,
    GmailAuthenticationError,
    GmailClient,
    GmailConfiguration,
    GmailConfigurationError,
    GmailPayloadError,
)

__all__ = [
    "GMAIL_READONLY_SCOPE",
    "GmailApiError",
    "GmailAuthenticationError",
    "GmailClient",
    "GmailConfiguration",
    "GmailConfigurationError",
    "GmailPayloadError",
]

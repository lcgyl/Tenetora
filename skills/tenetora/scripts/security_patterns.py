"""Shared credential detection patterns with explicit strictness levels."""

from __future__ import annotations

import re


# High-confidence staged-content scan. Keep the longer prefix/value thresholds
# used by commit guards so ordinary placeholder text is not treated as a secret.
HIGH_CONFIDENCE_SECRET_PATTERN = re.compile(
    r"(?:glpat-[A-Za-z0-9._-]{16,}|ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD)\b\s*[:=]\s*['\"]?[A-Za-z0-9+/._=-]{16,})",
    re.IGNORECASE,
)

# General project-content scan. This intentionally catches shorter assigned
# values because project rule/config imports need a stricter content boundary.
GENERAL_SECRET_PATTERN = re.compile(
    r"(?:glpat-[A-Za-z0-9._-]+|ghp_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
    r"sk-[A-Za-z0-9_-]{16,}|"
    r"-{0,5}BEGIN [A-Z ]*PRIVATE KEY-{0,5}|"
    r"\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PRIVATE_KEY|ACCESS_KEY)\b\s*[:=]\s*['\"]?[^'\"\s${}]+)",
    re.IGNORECASE,
)

JWT_SECRET_PATTERN = re.compile(
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

GENERAL_SECRET_PATTERNS = (GENERAL_SECRET_PATTERN, JWT_SECRET_PATTERN)

# Cross-module evidence scan. The minimum length matches the existing
# producer/consumer contract validator and remains distinct from both guards.
CONTENT_SECRET_PATTERN = re.compile(
    r"(?:glpat-[A-Za-z0-9._-]{16,}|ghp_[A-Za-z0-9_]{20,}|"
    r"\b(?:TOKEN|SECRET|PASSWORD)\b\s*[:=]\s*['\"]?\S{12,})",
    re.IGNORECASE,
)


__all__ = [
    "CONTENT_SECRET_PATTERN",
    "GENERAL_SECRET_PATTERN",
    "GENERAL_SECRET_PATTERNS",
    "HIGH_CONFIDENCE_SECRET_PATTERN",
    "JWT_SECRET_PATTERN",
]

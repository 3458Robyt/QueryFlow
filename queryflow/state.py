from __future__ import annotations

from typing import Any


VALIDATION_STATUSES = frozenset(
    {
        "prechecked",
        "ready",
        "changes_required",
        "blocked_vpc",
        "blocked_permission",
        "failed",
    }
)

WORKFLOW_STATES = frozenset(
    {
        "draft",
        "changed",
        "prechecked",
        "validating",
        "ready",
        "approved",
        "published",
        "exception_prepared",
        "changes_required",
        "blocked_vpc",
        "blocked_permission",
        "conflict",
        "failed",
    }
)


def validation_is_publishable(validation: dict[str, Any]) -> bool:
    """Return whether a validation artifact can authorize a copy operation."""
    return bool(
        validation.get("ok")
        and validation.get("publishable")
        and validation.get("status") == "ready"
        and validation.get("method") == "bigquery_dry_run"
    )


def normalize_validation_status(validation: dict[str, Any]) -> str:
    """Infer a status for legacy validation payloads."""
    current = validation.get("status")
    if isinstance(current, str) and current in VALIDATION_STATUSES:
        return current
    dry_run = validation.get("dry_run") or {}
    if dry_run.get("skipped"):
        return "prechecked"
    static = validation.get("static") or {}
    if static.get("errors"):
        return "changes_required"
    error_kind = validation.get("error_kind") or dry_run.get("error_kind")
    if error_kind == "vpc":
        return "blocked_vpc"
    if error_kind in {"permission", "authentication"}:
        return "blocked_permission"
    if error_kind == "sql":
        return "changes_required"
    if validation.get("ok") and dry_run.get("dry_run_ok") is True:
        return "ready"
    errors = [str(error).lower() for error in dry_run.get("errors") or []]
    if any("vpc service controls" in error or "organization's policy" in error for error in errors):
        return "blocked_vpc"
    if any("permission" in error or "access denied" in error for error in errors):
        return "blocked_permission"
    return "failed"


def task_state(
    manifest: dict[str, Any],
    validation: dict[str, Any] | None = None,
    *,
    current_sha256: str | None = None,
) -> str:
    """Derive the safe user-facing state from task artifacts."""
    if manifest.get("published"):
        return "published"
    if manifest.get("exception_digest"):
        return "exception_prepared"
    validation = validation or {}
    stored_sha = validation.get("content_sha256") or manifest.get("proposed_sha256")
    if current_sha256 and stored_sha and current_sha256 != stored_sha:
        return "changed"
    if validation:
        status = normalize_validation_status(validation)
        if status in VALIDATION_STATUSES:
            return status
    baseline = manifest.get("baseline_sha256")
    proposed = manifest.get("proposed_sha256")
    if baseline and proposed and baseline != proposed:
        return "changed"
    return "draft"

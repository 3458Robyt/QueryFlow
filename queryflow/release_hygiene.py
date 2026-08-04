"""Checks that public releases do not contain operational company artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HygieneFinding:
    rule: str
    path: str
    message: str


EXCLUDED_PARTS = {
    ".git",
    "__pycache__",
    "tasks",
    "reports",
    "pilot",
    "Diccionario.txt",
    "rewrite_notebook_routes.py",
    "migrate_notebooks_query.py",
}
INTERNAL_PATTERNS = (
    re.compile(r"analytics-\d+", re.IGNORECASE),
    re.compile(r"sbscol-[a-z0-9-]+", re.IGNORECASE),
    re.compile(r"sbs" + r"_analytics", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?:ghp|github_pat|AIza|xoxb)-[A-Za-z0-9_-]+"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),
)


def inspect_paths(root: Path) -> list[HygieneFinding]:
    findings: list[HygieneFinding] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            findings.append(HygieneFinding("excluded_path", str(relative), "Artefacto operativo excluido"))
            continue
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in INTERNAL_PATTERNS:
            if pattern.search(text):
                rule = "secret" if "PRIVATE KEY" in pattern.pattern or "Bearer" in pattern.pattern or "ghp" in pattern.pattern else "internal_identifier"
                findings.append(HygieneFinding(rule, str(relative), "Contenido sensible o identificador interno detectado"))
                break
    return findings

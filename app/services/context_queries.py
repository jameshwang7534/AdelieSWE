"""Deterministic, bounded title/body queries; no language-model dependency."""

import re


def issue_queries(title: str, body: str | None, count: int, chars: int) -> list[str]:
    queries: list[str] = []
    # Caller supplies a bounded issue excerpt. Favor title, explicit code, then body lines.
    text = body or ""
    candidates = [title, *re.findall(r"`([^`\n]+)`", text), *text.splitlines()]
    for candidate in candidates:
        normalized = " ".join(candidate.strip("# -*>").split())[:chars]
        if normalized and any(c.isalnum() for c in normalized) and normalized not in queries:
            queries.append(normalized)
        if len(queries) == count:
            break
    return queries

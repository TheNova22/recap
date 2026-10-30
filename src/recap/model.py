"""Shared in-memory report records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class Activity:
    id: str
    source: str
    kind: str
    at: str
    title: str
    detail: str = ""
    url: str = ""
    repo: str = ""
    summary: str = ""
    issue_key: str = ""
    group_id: str = ""
    include_in_daily_update: bool = True

    def to_dict(self) -> dict:
        # The browser only needs a summary, link, and assignment controls.
        # Raw agent excerpts stay in the server process.
        return {
            "id": self.id,
            "source": self.source,
            "kind": self.kind,
            "at": self.at,
            "summary": self.summary,
            "url": self.url,
            "issue_key": self.issue_key,
            "include_in_daily_update": self.include_in_daily_update,
        }


@dataclass(slots=True)
class Issue:
    key: str
    title: str
    status: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Report:
    date: str
    timezone: str
    items: list[Activity] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    daily_text: str = ""
    comments: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    daily_plain_text: str = ""

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "timezone": self.timezone,
            "items": [x.to_dict() for x in self.items],
            "issues": [x.to_dict() for x in self.issues],
            "daily_text": self.daily_text,
            "daily_plain_text": self.daily_plain_text,
            "comments": self.comments,
            "warnings": self.warnings,
        }

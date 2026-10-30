"""Issue tracker interface and Jira Cloud implementation."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import date, timedelta
from typing import Protocol

from .config import Config
from .model import Issue
from .sources import SourceError, http_json

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def _adf_text_nodes(value: str) -> list[dict]:
    nodes: list[dict] = []
    cursor = 0
    for match in URL_RE.finditer(value):
        raw_url = match.group(0)
        url = raw_url.rstrip(".,;:!?\"'")
        for closing, opening in ((")", "("), ("]", "["), ("}", "{")):
            while url.endswith(closing) and url.count(closing) > url.count(opening):
                url = url[:-1]
        if not url:
            continue
        link_end = match.start() + len(url)
        if match.start() > cursor:
            nodes.append({"type": "text", "text": value[cursor : match.start()]})
        nodes.append(
            {
                "type": "text",
                "text": url,
                "marks": [{"type": "link", "attrs": {"href": url}}],
            }
        )
        if link_end < match.end():
            nodes.append({"type": "text", "text": value[link_end : match.end()]})
        cursor = match.end()
    if cursor < len(value):
        nodes.append({"type": "text", "text": value[cursor:]})
    return nodes or ([{"type": "text", "text": value}] if value else [])


class IssueTracker(Protocol):
    name: str

    def find_candidates(
        self, day: date, keys: set[str], warnings: list[str]
    ) -> list[Issue]: ...

    def post_comment(self, issue_key: str, text: str) -> None: ...


class JiraIssueTracker:
    name = "jira"

    def __init__(self, config: Config):
        self.config = config

    def _credentials(self) -> tuple[str, str]:
        username = os.environ.get("JIRA_USERNAME", "")
        token = os.environ.get("JIRA_API_TOKEN", "")
        if not all((self.config.jira_base_url, username, token)):
            raise SourceError("Jira credentials or URL are missing")
        return username, token

    def find_candidates(
        self, day: date, keys: set[str], warnings: list[str]
    ) -> list[Issue]:
        try:
            credentials = self._credentials()
        except SourceError:
            warnings.append(
                "Jira skipped: configure jira_base_url, JIRA_USERNAME, and JIRA_API_TOKEN"
            )
            return []
        since = day - timedelta(days=self.config.jira_recent_days)
        key_clause = f" OR key in ({','.join(sorted(keys))})" if keys else ""
        jql = f'assignee = currentUser() AND statusCategory != Done AND ((created >= "{since}" OR updated >= "{since}"){key_clause}) ORDER BY updated DESC'
        issues: list[Issue] = []
        next_token = ""
        seen_tokens: set[str] = set()
        while True:
            payload: dict = (
                {
                    "jql": jql,
                    "maxResults": 100,
                    "fields": ["summary", "status"],
                    "nextPageToken": next_token,
                }
                if next_token
                else {"jql": jql, "maxResults": 100, "fields": ["summary", "status"]}
            )
            result = http_json(
                self.config.jira_base_url + "/rest/api/3/search/jql",
                basic=credentials,
                body=payload,
            )
            if not isinstance(result, dict):
                warnings.append(
                    "Jira issue search returned an unexpected page; results may be incomplete"
                )
                break
            for raw in result.get("issues", []):
                key = raw.get("key", "")
                fields = raw.get("fields") or {}
                issues.append(
                    Issue(
                        key,
                        fields.get("summary", ""),
                        (fields.get("status") or {}).get("name", ""),
                        f"{self.config.jira_base_url}/browse/{key}",
                    )
                )
            next_token = result.get("nextPageToken", "")
            if not next_token:
                break
            if next_token in seen_tokens:
                warnings.append(
                    "Jira issue search repeated a pagination token; results may be incomplete"
                )
                break
            seen_tokens.add(next_token)
        return issues

    def post_comment(self, issue_key: str, text: str) -> None:
        credentials = self._credentials()
        if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", issue_key):
            raise ValueError("Invalid Jira issue key")
        content: list[dict] = []
        bullets: list[dict] = []

        def flush_bullets() -> None:
            if bullets:
                content.append({"type": "bulletList", "content": list(bullets)})
                bullets.clear()

        for line in text.splitlines():
            if line.startswith("- "):
                paragraph = {"type": "paragraph", "content": _adf_text_nodes(line[2:])}
                bullets.append({"type": "listItem", "content": [paragraph]})
            else:
                flush_bullets()
                if line.strip():
                    content.append(
                        {"type": "paragraph", "content": _adf_text_nodes(line)}
                    )
        flush_bullets()
        body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": content or [{"type": "paragraph", "content": []}],
            }
        }
        http_json(
            self.config.jira_base_url + f"/rest/api/3/issue/{issue_key}/comment",
            basic=credentials,
            body=body,
        )


ISSUE_TRACKER_FACTORIES: dict[str, Callable[[Config], IssueTracker]] = {
    JiraIssueTracker.name: JiraIssueTracker
}


def create_issue_tracker(config: Config) -> IssueTracker:
    factory = ISSUE_TRACKER_FACTORIES.get(config.issue_provider)
    if factory is None:
        raise ValueError(f"Unknown issue provider: {config.issue_provider}")
    return factory(config)

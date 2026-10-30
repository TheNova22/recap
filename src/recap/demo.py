"""Synthetic data and providers used by the local capture harness."""

from __future__ import annotations

from datetime import date

from .config import Config
from .model import Activity, Issue, Report
from .report import render_report

DEMO_DAY = date(2026, 1, 15)
DEMO_MODEL = "demo-model"
DEMO_AI_ENDPOINT = "Demo fixture; no AI request is sent"


def demo_issues() -> list[Issue]:
    return [
        Issue(
            "MOCK-123",
            "Harden webhook retry behavior",
            "In Progress",
            "https://jira.example.invalid/browse/MOCK-123",
        ),
        Issue(
            "MOCK-124",
            "Add delivery observability",
            "To Do",
            "https://jira.example.invalid/browse/MOCK-124",
        ),
    ]


def build_demo_report(day: date = DEMO_DAY, config: Config | None = None) -> Report:
    """Build a fresh report with realistic-looking, entirely synthetic activity."""
    config = config or Config(timezone="UTC", ai_model=DEMO_MODEL)
    day_text = day.isoformat()
    report = Report(
        date=day_text,
        timezone="UTC",
        items=[
            Activity(
                id="demo-pr-review-42",
                source="github",
                kind="PullRequestGroup",
                at=f"{day_text}T09:20:00Z",
                title="PR #42 — Harden webhook retry behavior",
                detail="Reviewed the retry policy, replied to feedback, and resolved a thread.",
                url="https://github.example.invalid/demo/webhooks/pull/42",
                repo="demo/webhooks",
                summary="Reviewed retry handling and resolved the backoff discussion.",
                issue_key="MOCK-123",
                group_id="demo-pr-42",
            ),
            Activity(
                id="demo-commit-7f3a2b1",
                source="local_git",
                kind="Commit",
                at=f"{day_text}T10:45:00Z",
                title="Add bounded retry backoff",
                detail="Commit 7f3a2b1",
                url="https://github.example.invalid/demo/webhooks/commit/7f3a2b1",
                repo="demo/webhooks",
                summary="Added bounded exponential backoff for webhook retries.",
                issue_key="MOCK-123",
            ),
            Activity(
                id="demo-agent-session",
                source="codex",
                kind="AgentSession",
                at=f"{day_text}T13:10:00Z",
                title="Session: webhook delivery metrics",
                detail="Synthetic agent-session excerpt for README captures.",
                repo="demo/observability",
                summary="Added delivery-attempt metrics and recovery notes.",
                issue_key="MOCK-124",
            ),
            Activity(
                id="demo-docs-update",
                source="local_git",
                kind="Commit",
                at=f"{day_text}T15:05:00Z",
                title="Update the operator guide",
                detail="Commit a90d14c",
                url="https://github.example.invalid/demo/observability/commit/a90d14c",
                repo="demo/observability",
                summary="Updated the operator guide with delivery-recovery steps.",
            ),
            Activity(
                id="demo-excluded-noise",
                source="github",
                kind="PullRequestEvent",
                at=f"{day_text}T16:00:00Z",
                title="Closed an outdated draft pull request",
                url="https://github.example.invalid/demo/webhooks/pull/19",
                repo="demo/webhooks",
                summary="Closed an outdated draft pull request.",
                include_in_daily_update=False,
            ),
        ],
        issues=demo_issues(),
    )
    render_report(report, config)
    return report


class DemoAIProvider:
    """AI-provider stand-in that never sends a request."""

    endpoint_url = DEMO_AI_ENDPOINT

    def list_models(self) -> list[str]:
        return [DEMO_MODEL]


class DemoIssueTracker:
    """Jira stand-in that returns mock tickets and records posts in memory."""

    name = "demo_jira"

    def __init__(self):
        self.posted_comments: list[tuple[str, str]] = []

    def find_candidates(
        self, _day: date, _keys: set[str], _warnings: list[str]
    ) -> list[Issue]:
        return demo_issues()

    def post_comment(self, issue_key: str, text: str) -> None:
        self.posted_comments.append((issue_key, text))


class DemoService:
    """RecapService-shaped stand-in used only by the capture harness."""

    def __init__(self, config: Config):
        self.config = config
        self.ai = DemoAIProvider()
        self.issues = DemoIssueTracker()

    def build(self, day: date) -> Report:
        return build_demo_report(day, self.config)

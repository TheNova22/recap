"""Report orchestration and editable text rendering."""

from __future__ import annotations

from datetime import date

from .ai_provider import AIProvider, create_ai_provider
from .config import Config
from .issue_provider import IssueTracker, create_issue_tracker
from .model import Activity, Report
from .providers import DEFAULT_PROVIDERS, ActivityProvider
from .sources import KEY_RE, SourceError, day_window, parse_time


def render_report(report: Report, config: Config) -> None:
    report.items.sort(key=lambda item: (item.at, item.id))
    included = [item for item in report.items if item.include_in_daily_update]
    heading = f"Daily update — {report.date} ({report.timezone})"
    if included:
        markdown_items = "\n".join(
            f"- {item.summary}" + (f" — {item.url}" if item.url else "")
            for item in included
        )
        plain_items = "\n".join(
            f"{item.summary}" + (f" ({item.url})" if item.url else "")
            for item in included
        )
    else:
        markdown_items = plain_items = "No activity found for this date."
    report.daily_text = f"# {heading}\n\n{markdown_items}"
    report.daily_plain_text = f"{heading}\n\n{plain_items}"
    issue_titles = {issue.key: issue.title for issue in report.issues}
    report.comments = {}
    template = config.templates[config.comment_template]
    for key in sorted(
        {
            item.issue_key
            for item in report.items
            if item.issue_key and item.include_in_daily_update
        }
    ):
        matches = [
            item
            for item in report.items
            if item.issue_key == key and item.include_in_daily_update
        ]
        bullets = "\n".join(f"- {item.summary}" for item in matches)
        links = (
            "\n".join(dict.fromkeys(item.url for item in matches if item.url))
            or "No external links"
        )
        variables = {
            **config.custom,
            "date": report.date,
            "issue_key": key,
            "issue_title": issue_titles.get(key, key),
            "items": bullets,
            "links": links,
        }
        try:
            report.comments[key] = template.format_map(variables).strip()
        except KeyError as exc:
            raise ValueError(f"Unknown comment template field: {exc}") from exc


def deduplicate(items: list[Activity]) -> list[Activity]:
    by_id: dict[str, Activity] = {}
    review_urls: set[str] = set()
    pr_actions: set[tuple[str, str]] = set()
    grouped_actions: set[tuple[str, str, str]] = set()
    for item in items:
        if item.kind == "PullRequestReview" and item.url:
            if item.url in review_urls:
                continue
            review_urls.add(item.url)
        if item.group_id and item.kind in ("PullRequest", "PullRequestEvent"):
            action_marker = (item.group_id, item.url, item.detail)
            if action_marker in grouped_actions:
                continue
            grouped_actions.add(action_marker)
        elif item.url and item.kind in ("PullRequest", "PullRequestEvent"):
            action = (
                "opened"
                if item.title.lower().startswith(("opened ", "open "))
                else "merged"
                if item.title.lower().startswith(("merged ", "closed "))
                else item.title.split(" ", 1)[0].lower()
            )
            marker = (item.url, action)
            if marker in pr_actions:
                continue
            pr_actions.add(marker)
        existing = by_id.get(item.id)
        if existing is None or (
            existing.source == "local_git" and item.source == "github"
        ):
            by_id[item.id] = item
    items = list(by_id.values())

    groups: dict[str, list[Activity]] = {}
    ungrouped: list[Activity] = []
    for item in items:
        if item.group_id:
            groups.setdefault(item.group_id, []).append(item)
        else:
            ungrouped.append(item)

    for group_id, group in groups.items():
        first = group[0]
        action_counts: dict[str, int] = {}
        for item in group:
            action = item.detail or item.kind
            action_counts[action] = action_counts.get(action, 0) + 1
        details = [
            f"{action} ({count} times)" if count > 1 else action
            for action, count in sorted(action_counts.items())
        ]
        latest = max(
            group,
            key=lambda item: parse_time(item.at) or parse_time("1970-01-01T00:00:00Z"),
        )
        first.id = group_id
        first.kind = "PullRequestGroup"
        first.at = latest.at
        first.detail = "; ".join(details)
        first.url = next((item.url.split("#", 1)[0] for item in group if item.url), "")
        ungrouped.append(first)

    return ungrouped


class RecapService:
    """Combine independently replaceable activity, AI, and issue providers."""

    def __init__(
        self,
        config: Config,
        *,
        activity_providers: tuple[ActivityProvider, ...] | None = None,
        ai_provider: AIProvider | None = None,
        issue_tracker: IssueTracker | None = None,
    ):
        self.config = config
        self.activity_providers = (
            activity_providers if activity_providers is not None else DEFAULT_PROVIDERS
        )
        self.ai = ai_provider if ai_provider is not None else create_ai_provider(config)
        self.issues = (
            issue_tracker if issue_tracker is not None else create_issue_tracker(config)
        )

    def build(self, day: date) -> Report:
        start, end, zone = day_window(day, self.config.timezone)
        report = Report(day.isoformat(), zone)
        if zone.startswith("local UTC"):
            report.warnings.append(
                "Could not identify the machine's IANA timezone; using its current UTC offset. Set timezone explicitly for historical dates."
            )
        collected: list[Activity] = []
        for provider in self.activity_providers:
            try:
                collected.extend(
                    provider.collect(self.config, day, start, end, report.warnings)
                )
            except SourceError as exc:
                report.warnings.append(f"{provider.name}: {exc}")
        report.items = deduplicate(collected)
        keys = (
            set().union(
                *(
                    set(
                        KEY_RE.findall(
                            item.title + " " + item.detail[:400] + " " + item.repo
                        )
                    )
                    for item in report.items
                )
            )
            if report.items
            else set()
        )
        try:
            report.issues = self.issues.find_candidates(day, keys, report.warnings)
        except SourceError as exc:
            report.warnings.append(f"{self.issues.name}: {exc}")
        if report.items:
            self.ai.review(report.items, report.issues)
        render_report(report, self.config)
        return report


def build_report(config: Config, day: date) -> Report:
    """Convenience entry point for callers that do not need provider injection."""
    return RecapService(config).build(day)

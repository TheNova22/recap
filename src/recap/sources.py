"""Read-only activity collectors for GitHub, local Git, agent logs, and Jira."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from .config import Config
from .model import Activity
from .timezones import resolve_timezone

KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
PR_EVENT_ACTIONS = {
    "opened",
    "reopened",
    "closed",
    "ready_for_review",
    "converted_to_draft",
    "synchronize",
}


class SourceError(RuntimeError):
    pass


def _url_origin(url: str) -> tuple[str, str, int] | None:
    """Return a normalized HTTP origin, or None for an invalid/non-HTTP URL."""
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if scheme not in {"http", "https"} or not hostname:
            return None
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname.lower(), port


class _StripCrossOriginAuthorization(urllib.request.HTTPRedirectHandler):
    """Prevent urllib's default redirect handler forwarding credentials."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _url_origin(req.full_url) != _url_origin(newurl):
            redirected.remove_header("Authorization")
        return redirected


def day_window(day: date, tz_name: str) -> tuple[datetime, datetime, str]:
    zone, label = resolve_timezone(tz_name)
    start = datetime.combine(day, time.min, zone)
    end = datetime.combine(day + timedelta(days=1), time.min, zone)
    return start.astimezone(UTC), end.astimezone(UTC), label


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        instant = datetime.fromisoformat(value)
        return (
            instant.replace(tzinfo=UTC)
            if instant.tzinfo is None
            else instant.astimezone(UTC)
        )
    except ValueError:
        return None


def in_window(value: str | None, start: datetime, end: datetime) -> bool:
    instant = parse_time(value)
    return instant is not None and start <= instant < end


def http_json(
    url: str,
    *,
    token: str = "",
    basic: tuple[str, str] | None = None,
    body: dict | None = None,
    timeout: int = 30,
) -> dict | list:
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-store",
        "User-Agent": "recap-eod/0.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        if "api.github.com" in url:
            headers["Accept"] = "application/vnd.github+json"
    if basic:
        raw = f"{basic[0]}:{basic[1]}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET"
    )
    try:
        opener = urllib.request.build_opener(_StripCrossOriginAuthorization())
        with opener.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SourceError(
            f"{urllib.parse.urlparse(url).hostname} returned HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SourceError(
            f"Could not read {urllib.parse.urlparse(url).hostname}: {exc}"
        ) from exc


def _github(token: str, path: str) -> dict | list:
    return http_json("https://api.github.com" + path, token=token)


def _github_search(token: str, query: str) -> tuple[list[dict], bool]:
    found: list[dict] = []
    truncated = False
    for page in range(1, 11):
        path = (
            "/search/issues?q="
            + urllib.parse.quote(query)
            + f"&per_page=100&page={page}"
        )
        result = _github(token, path)
        if not isinstance(result, dict):
            break
        batch = result.get("items", [])
        found.extend(batch)
        truncated = (
            truncated
            or bool(result.get("incomplete_results"))
            or result.get("total_count", 0) > 1000
        )
        if len(batch) < 100:
            break
    return found, truncated


def collect_github(
    day: date, start: datetime, end: datetime, warnings: list[str]
) -> list[Activity]:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        warnings.append("GitHub skipped: GITHUB_TOKEN is unset")
        return []
    user = _github(token, "/user")
    login = user.get("login", "") if isinstance(user, dict) else ""
    if not login:
        raise SourceError("GitHub did not identify the authenticated user")
    if day < datetime.now(UTC).date() - timedelta(days=29):
        warnings.append(
            "GitHub event history does not cover dates older than about 30 days"
        )
    items: list[Activity] = []
    events: list[dict] = []
    for page in range(1, 4):
        try:
            result = _github(
                token,
                f"/users/{urllib.parse.quote(login)}/events?per_page=100&page={page}",
            )
        except SourceError as exc:
            warnings.append(f"GitHub events unavailable: {exc}")
            break
        if not isinstance(result, list) or not result:
            break
        events.extend(result)
        if len(result) < 100:
            break
    if len(events) >= 300 or (
        events
        and parse_time(events[-1].get("created_at"))
        and parse_time(events[-1].get("created_at")) > start
    ):
        warnings.append("GitHub event history may be truncated before the selected day")
    warnings.append(
        "GitHub push events may arrive hours late; refresh later if recent pushes are missing"
    )
    for event in events:
        at = event.get("created_at")
        if not in_window(at, start, end):
            continue
        kind = event.get("type", "")
        payload = event.get("payload") or {}
        repo = (event.get("repo") or {}).get("name", "")
        title = ""
        url = ""
        detail = ""
        group_id = ""
        if kind == "PushEvent":
            ref = (payload.get("ref") or "").removeprefix("refs/heads/")
            title = f"Pushed {ref} to {repo}"
            head = payload.get("head", "")
            url = (
                f"https://github.com/{repo}/commit/{head}"
                if head
                else f"https://github.com/{repo}"
            )
        elif kind == "PullRequestEvent":
            pr = payload.get("pull_request") or {}
            action = payload.get("action", "updated")
            if action not in PR_EVENT_ACTIONS:
                continue
            number = pr.get("number", "")
            title = f"PR #{number}: {pr.get('title', '')}"
            url = pr.get("html_url", "")
            group_id = f"github:pr:{repo}:{number}" if repo and number else ""
            if action == "closed" and pr.get("merged_at"):
                detail = "PR merged"
            else:
                detail = {
                    "opened": "PR opened",
                    "reopened": "PR reopened",
                    "closed": "PR closed",
                    "ready_for_review": "PR marked ready for review",
                    "converted_to_draft": "PR converted to draft",
                    "synchronize": "PR updated with commits",
                }[action]
        elif kind == "PullRequestReviewEvent":
            pr = payload.get("pull_request") or {}
            review = payload.get("review") or {}
            if review.get("user", {}).get("login", "").lower() != login.lower():
                continue
            at = review.get("submitted_at") or at
            if not in_window(at, start, end):
                continue
            number = pr.get("number", "")
            title = f"PR #{number}: {pr.get('title', '')}"
            url = review.get("html_url") or pr.get("html_url", "")
            detail = f"Review submitted: {review.get('state', 'commented').lower()}"
            group_id = f"github:pr:{repo}:{number}" if repo and number else ""
        elif kind in (
            "IssueCommentEvent",
            "PullRequestReviewCommentEvent",
            "CommitCommentEvent",
        ):
            issue = payload.get("issue") or payload.get("pull_request") or {}
            comment = payload.get("comment") or {}
            url = comment.get("html_url", "")
            pr = payload.get("pull_request") or issue.get("pull_request") or {}
            number = pr.get("number") or issue.get("number", "")
            is_pr = bool(pr) or kind == "PullRequestReviewCommentEvent"
            if is_pr and number:
                title = f"PR #{number}: {pr.get('title') or issue.get('title', '')}"
                group_id = f"github:pr:{repo}:{number}" if repo else ""
                detail = (
                    "Replied to a review comment"
                    if kind == "PullRequestReviewCommentEvent"
                    else "Commented on the PR"
                )
                url = pr.get("html_url") or issue.get("html_url") or url
            else:
                title = f"Commented on {issue.get('title', repo)}"
                detail = (
                    "Commented on a commit"
                    if kind == "CommitCommentEvent"
                    else "Commented on an issue"
                )
        if title:
            activity_id = f"github:event:{event.get('id')}"
            if kind == "PullRequestReviewEvent" and review.get("id") is not None:
                activity_id = f"github:review:{review['id']}"
            items.append(
                Activity(
                    activity_id,
                    "github",
                    kind,
                    at,
                    title,
                    detail,
                    url,
                    repo,
                    group_id=group_id,
                )
            )
    date_query = f"{start.isoformat()}..{end.isoformat()}"
    # Search identifies candidates; actual review timestamps are checked below.
    for query in (
        f"type:pr author:{login} updated:{date_query}",
        f"type:pr reviewed-by:{login} updated:{date_query}",
    ):
        try:
            found, truncated = _github_search(token, query)
        except SourceError as exc:
            warnings.append(f"GitHub PR search unavailable: {exc}")
            continue
        if truncated:
            warnings.append("GitHub PR search returned incomplete or capped results")
        for pr in found:
            repo_url = pr.get("repository_url", "")
            repo = repo_url.removeprefix("https://api.github.com/repos/")
            number = pr.get("number")
            if not repo or not number:
                continue
            if pr.get("user", {}).get(
                "login", ""
            ).lower() == login.lower() and in_window(pr.get("created_at"), start, end):
                items.append(
                    Activity(
                        f"github:pr:{repo}:{number}:created",
                        "github",
                        "PullRequest",
                        pr["created_at"],
                        f"PR #{number}: {pr.get('title', '')}",
                        "PR opened",
                        pr.get("html_url", ""),
                        repo,
                        group_id=f"github:pr:{repo}:{number}",
                    )
                )
            merged_at = (pr.get("pull_request") or {}).get("merged_at")
            if pr.get("user", {}).get(
                "login", ""
            ).lower() == login.lower() and in_window(merged_at, start, end):
                items.append(
                    Activity(
                        f"github:pr:{repo}:{number}:merged",
                        "github",
                        "PullRequest",
                        merged_at,
                        f"PR #{number}: {pr.get('title', '')}",
                        "PR merged",
                        pr.get("html_url", ""),
                        repo,
                        group_id=f"github:pr:{repo}:{number}",
                    )
                )
            if "reviewed-by:" in query:
                for review_page in range(1, 11):
                    try:
                        reviews = _github(
                            token,
                            f"/repos/{repo}/pulls/{number}/reviews?per_page=100&page={review_page}",
                        )
                    except SourceError as exc:
                        warnings.append(
                            f"Reviews unavailable for {repo}#{number}: {exc}"
                        )
                        break
                    if not isinstance(reviews, list):
                        break
                    for review in reviews:
                        at = review.get("submitted_at")
                        if review.get("user", {}).get(
                            "login", ""
                        ).lower() == login.lower() and in_window(at, start, end):
                            items.append(
                                Activity(
                                    f"github:review:{review.get('id')}",
                                    "github",
                                    "PullRequestReview",
                                    at,
                                    f"PR #{number}: {pr.get('title', '')}",
                                    f"Review submitted: {review.get('state', 'commented').lower()}",
                                    review.get("html_url", ""),
                                    repo,
                                    group_id=f"github:pr:{repo}:{number}",
                                )
                            )
                    if len(reviews) < 100:
                        break
                else:
                    warnings.append(
                        f"Review list for {repo}#{number} reached its page limit"
                    )
    # Commits authored or committed by this account can live outside local roots.
    for role in ("author", "committer"):
        found: list[dict] = []
        for page in range(1, 11):
            query = f"{role}:{login} {role}-date:{date_query}"
            try:
                result = _github(
                    token,
                    "/search/commits?q="
                    + urllib.parse.quote(query)
                    + f"&per_page=100&page={page}",
                )
            except SourceError as exc:
                warnings.append(f"GitHub commit search unavailable: {exc}")
                break
            if not isinstance(result, dict):
                break
            found.extend(result.get("items", []))
            if result.get("incomplete_results") or result.get("total_count", 0) > 1000:
                warnings.append(
                    "GitHub commit search returned incomplete or capped results"
                )
            if len(result.get("items", [])) < 100:
                break
        for commit in found:
            meta = commit.get("commit") or {}
            at = (meta.get(role) or {}).get("date")
            if not in_window(at, start, end):
                continue
            repo = (commit.get("repository") or {}).get("full_name", "")
            sha = commit.get("sha", "")
            title = (meta.get("message") or "").splitlines()[0]
            items.append(
                Activity(
                    f"commit:{repo}:{sha}",
                    "github",
                    "Commit",
                    at,
                    title,
                    "",
                    commit.get("html_url", ""),
                    repo,
                )
            )
    return items


def discover_repos(config: Config, warnings: list[str]) -> list[Path]:
    found: set[Path] = set()
    excluded = set(config.repo_excludes) | {".git"}
    for root in config.repo_roots:
        if not root.is_dir():
            warnings.append(f"Repository root is unavailable: {root}")
            continue
        for current, dirs, _files in os.walk(
            root,
            followlinks=False,
            onerror=lambda error: warnings.append(
                f"Cannot scan {error.filename}: {error.strerror}"
            ),
        ):
            here = Path(current)
            if (here / ".git").exists():
                found.add(here)
            dirs[:] = [
                name
                for name in dirs
                if name not in excluded
                and not name.startswith(".")
                and not (here / name).is_symlink()
            ]
    return sorted(found)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise SourceError(
            f"Git command failed in {repo}: {result.stderr.strip()[:200]}"
        )
    return result.stdout.strip()


def collect_local_git(
    config: Config, start: datetime, end: datetime, warnings: list[str]
) -> list[Activity]:
    items: list[Activity] = []
    for repo in discover_repos(config, warnings):
        try:
            email = _git(repo, "config", "user.email").lower()
            if not email:
                warnings.append(
                    f"Local commits skipped in {repo}: git user.email is unset"
                )
                continue
            output = _git(
                repo,
                "log",
                "--all",
                f"--since={start.isoformat()}",
                f"--until={end.isoformat()}",
                "--format=%H%x1f%cI%x1f%ae%x1f%s",
            )
            try:
                remote = _git(repo, "remote", "get-url", "origin")
            except SourceError:
                remote = ""
            match = re.search(r"(?:github\.com[:/])([^/]+/[^/]+?)(?:\.git)?$", remote)
            repo_name = match.group(1) if match else str(repo)
            for line in output.splitlines():
                fields = line.split("\x1f", 3)
                if len(fields) != 4:
                    continue
                sha, at, author_email, title = fields
                if author_email.lower() != email or not in_window(at, start, end):
                    continue
                url = f"https://github.com/{repo_name}/commit/{sha}" if match else ""
                items.append(
                    Activity(
                        f"commit:{repo_name}:{sha}",
                        "local_git",
                        "Commit",
                        at,
                        title,
                        f"Local commit in {repo}",
                        url,
                        repo_name,
                    )
                )
        except (SourceError, subprocess.TimeoutExpired) as exc:
            warnings.append(str(exc))
    return items


def _text_blocks(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (
                "text",
                "output_text",
            ):
                text = block.get("text", block.get("content", ""))
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _session_file(
    path: Path, source: str, start: datetime, end: datetime
) -> list[Activity]:
    rows: list[tuple[str, str, str]] = []
    cwd = ""
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # An agent may still be writing the final line.
            if source == "codex":
                if row.get("type") == "session_meta":
                    cwd = (row.get("payload") or {}).get("cwd", "")
                payload = row.get("payload") or {}
                if (
                    row.get("type") != "response_item"
                    or payload.get("type") != "message"
                ):
                    continue
                role = payload.get("role", "")
                message = _text_blocks(payload.get("content"))
                at = row.get("timestamp", "")
            else:
                raw_message = row.get("message")
                message_row = raw_message if isinstance(raw_message, dict) else {}
                role = row.get("role") or message_row.get("role") or row.get("type", "")
                if role not in ("user", "assistant") or row.get("isSidechain"):
                    continue
                cwd = row.get("cwd") or cwd
                content = message_row.get(
                    "content",
                    row.get(
                        "content",
                        raw_message
                        if isinstance(raw_message, str)
                        else row.get("text", ""),
                    ),
                )
                message = _text_blocks(content)
                at = (
                    row.get("timestamp")
                    or row.get("createdAt")
                    or row.get("created_at", "")
                )
            when = parse_time(at)
            if message and when and when < end:
                rows.append((role, at, message))
    if not rows:
        return []
    # Keep the last assistant message in each user turn, paired with its prompt.
    prompt = ""
    output: list[Activity] = []
    last_answer: tuple[int, str, str] | None = None
    for index, (role, at, message) in enumerate(rows):
        if role == "user":
            if last_answer:
                answer_index, answer_at, answer_text = last_answer
                if in_window(answer_at, start, end):
                    title = (
                        prompt.splitlines()[0][:140]
                        if prompt
                        else f"{source.title()} session in {Path(cwd).name or 'workspace'}"
                    )
                    output.append(
                        Activity(
                            f"{source}:{path.stem}:{answer_index}",
                            source,
                            "AgentSession",
                            answer_at,
                            title,
                            answer_text[:2500],
                            "",
                            cwd,
                        )
                    )
                last_answer = None
            prompt = message[:350]
        elif role == "assistant" and message.strip():
            last_answer = (index, at, message)
    if last_answer:
        answer_index, answer_at, answer_text = last_answer
        if in_window(answer_at, start, end):
            title = (
                prompt.splitlines()[0][:140]
                if prompt
                else f"{source.title()} session in {Path(cwd).name or 'workspace'}"
            )
            output.append(
                Activity(
                    f"{source}:{path.stem}:{answer_index}",
                    source,
                    "AgentSession",
                    answer_at,
                    title,
                    answer_text[:2500],
                    "",
                    cwd,
                )
            )
    return output


def collect_agent_sessions(
    start: datetime,
    end: datetime,
    warnings: list[str],
    enabled_sources: list[str] | None = None,
) -> list[Activity]:
    roots_by_source = {
        "codex": Path.home() / ".codex" / "sessions",
        "claude": Path.home() / ".claude" / "projects",
        "cursor": Path.home() / ".cursor" / "projects",
    }
    selected = enabled_sources if enabled_sources is not None else list(roots_by_source)
    output: list[Activity] = []
    for source in selected:
        root = roots_by_source.get(source)
        if root is None:
            continue
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                if (
                    "subagents" in path.parts
                    or path.stat().st_mtime < start.timestamp()
                ):
                    continue
                output.extend(_session_file(path, source, start, end))
            except (OSError, UnicodeError) as exc:
                warnings.append(f"Could not read {source} session {path.name}: {exc}")
    return output

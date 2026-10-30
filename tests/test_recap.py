import json
import re
import sys
import threading
import time
import tomllib
from datetime import UTC, date, datetime
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from recap import ai_provider, cli, demo, issue_provider, report, sources, webui
from recap import config as config_module
from recap.config import DEFAULT_TEXT, Config, default_config_path, parse_config
from recap.model import Activity, Issue, Report
from recap.tui import RecapTUI
from recap.webui import make_server


def test_config_allows_roots_and_rejects_secrets():
    config = parse_config(
        DEFAULT_TEXT.replace(
            'repo_roots = ["~/work"]', 'repo_roots = ["~/code", "~/work"]'
        )
    )
    assert len(config.repo_roots) == 2
    with pytest.raises(ValueError, match="Secrets"):
        parse_config('repo_roots = []\napi_key = "do-not-save"\n')


def test_new_configs_leave_model_selection_empty():
    assert Config().ai_model == ""
    assert parse_config("repo_roots = []\n").ai_model == ""


def test_local_config_init_command_creates_a_usable_file(tmp_path):
    path = tmp_path / "config.toml"
    assert cli.main(["--config", str(path), "config", "init"]) == 0
    assert parse_config(path.read_text()).ai_provider == "openai_responses"


def test_repo_roots_recursively_discover_nested_repos_and_worktrees(tmp_path):
    root = tmp_path / "projects"
    (root / "one" / ".git").mkdir(parents=True)
    (root / "team" / "two" / ".git").mkdir(parents=True)
    worktree = root / "team" / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: ../.git/worktrees/worktree\n")
    (root / "node_modules" / "hidden" / ".git").mkdir(parents=True)
    warnings = []
    found = sources.discover_repos(
        Config(repo_roots=[root, root / "team"], repo_excludes=["node_modules"]),
        warnings,
    )
    assert found == sorted([root / "one", root / "team" / "two", worktree])
    assert warnings == []


def test_repos_command_lists_discovered_repositories(tmp_path, capsys):
    root = tmp_path / "projects"
    repo = root / "team" / "service"
    (repo / ".git").mkdir(parents=True)
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'repo_roots = ["{root.as_posix()}"]\n')
    assert cli.main(["--config", str(config_path), "repos"]) == 0
    assert str(repo) in capsys.readouterr().out


def test_day_window_handles_daylight_saving_boundary():
    start, end, zone = sources.day_window(date(2026, 3, 8), "America/New_York")
    assert (end - start).total_seconds() == 23 * 3600
    assert zone == "America/New_York"
    assert sources.in_window("2026-03-09T03:59:59Z", start, end)
    assert not sources.in_window("2026-03-09T04:00:00Z", start, end)


def test_local_timezone_uses_machine_tz_setting(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    start, end, label = sources.day_window(date(2026, 10, 27), "local")
    assert label == "Asia/Kolkata"
    assert start.isoformat() == "2026-10-26T18:30:00+00:00"
    assert end.isoformat() == "2026-10-27T18:30:00+00:00"
    with pytest.raises(ValueError, match="IANA timezone"):
        parse_config('timezone = "IST"\n')


def test_claude_session_uses_last_answer_per_turn(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    rows = [
        {
            "type": "user",
            "timestamp": "2026-10-27T09:00:00Z",
            "message": {"content": "Fix AAP-123 bug"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-10-27T09:01:00Z",
            "message": {"content": [{"type": "text", "text": "Working on it"}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-10-27T09:02:00Z",
            "message": {"content": [{"type": "text", "text": "Fixed the bug"}]},
        },
        {
            "type": "user",
            "timestamp": "2026-10-27T10:00:00Z",
            "message": {"content": "Review the PR"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-10-27T10:02:00Z",
            "message": {"content": [{"type": "text", "text": "Reviewed it"}]},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n{incomplete")
    items = sources._session_file(
        path,
        "claude",
        datetime(2026, 10, 27, tzinfo=UTC),
        datetime(2026, 10, 28, tzinfo=UTC),
    )
    assert [item.detail for item in items] == ["Fixed the bug", "Reviewed it"]
    assert items[0].title == "Fix AAP-123 bug"


def test_review_search_checks_submission_date(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-value")
    pr = {
        "number": 42,
        "title": "A fix",
        "repository_url": "https://api.github.com/repos/o/r",
        "html_url": "https://github.com/o/r/pull/42",
        "user": {"login": "other"},
    }

    def fake_search(_token, query):
        return ([pr], False) if "reviewed-by:" in query else ([], False)

    def fake_github(_token, path):
        if path == "/user":
            return {"login": "me"}
        if "/events?" in path:
            return []
        if "/reviews?" in path:
            return [
                {
                    "id": 1,
                    "user": {"login": "me"},
                    "submitted_at": "2026-10-26T11:00:00Z",
                    "html_url": "https://github.com/o/r/pull/42#pullrequestreview-1",
                },
                {
                    "id": 2,
                    "user": {"login": "me"},
                    "submitted_at": "2026-10-27T11:00:00Z",
                    "html_url": "https://github.com/o/r/pull/42#pullrequestreview-2",
                },
            ]
        return {"items": [], "total_count": 0}

    monkeypatch.setattr(sources, "_github_search", fake_search)
    monkeypatch.setattr(sources, "_github", fake_github)
    start = datetime(2026, 10, 27, tzinfo=UTC)
    end = datetime(2026, 10, 28, tzinfo=UTC)
    items = sources.collect_github(date(2026, 10, 27), start, end, [])
    assert [item.id for item in items] == ["github:review:2"]


def test_github_filters_metadata_and_groups_pr_work(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-value")
    pr = {"number": 42, "title": "A fix", "html_url": "https://github.com/o/r/pull/42"}
    events = [
        {
            "id": "label",
            "type": "PullRequestEvent",
            "created_at": "2026-10-27T09:00:00Z",
            "repo": {"name": "o/r"},
            "payload": {"action": "labeled", "pull_request": pr},
        },
        {
            "id": "sync",
            "type": "PullRequestEvent",
            "created_at": "2026-10-27T09:01:00Z",
            "repo": {"name": "o/r"},
            "payload": {"action": "synchronize", "pull_request": pr},
        },
        {
            "id": "review",
            "type": "PullRequestReviewEvent",
            "created_at": "2026-10-27T09:02:00Z",
            "repo": {"name": "o/r"},
            "payload": {
                "pull_request": pr,
                "review": {
                    "user": {"login": "me"},
                    "submitted_at": "2026-10-27T09:02:00Z",
                    "state": "APPROVED",
                    "html_url": "https://github.com/o/r/pull/42#pullrequestreview-3",
                },
            },
        },
        {
            "id": "comment",
            "type": "IssueCommentEvent",
            "created_at": "2026-10-27T09:03:00Z",
            "repo": {"name": "o/r"},
            "payload": {
                "issue": {"number": 42, "title": "A fix", "pull_request": pr},
                "comment": {
                    "html_url": "https://github.com/o/r/pull/42#issuecomment-4"
                },
            },
        },
        {
            "id": "comment-again",
            "type": "IssueCommentEvent",
            "created_at": "2026-10-27T09:03:30Z",
            "repo": {"name": "o/r"},
            "payload": {
                "issue": {"number": 42, "title": "A fix", "pull_request": pr},
                "comment": {
                    "html_url": "https://github.com/o/r/pull/42#issuecomment-6"
                },
            },
        },
        {
            "id": "reply",
            "type": "PullRequestReviewCommentEvent",
            "created_at": "2026-10-27T09:04:00Z",
            "repo": {"name": "o/r"},
            "payload": {
                "pull_request": pr,
                "comment": {"html_url": "https://github.com/o/r/pull/42#discussion_r5"},
            },
        },
    ]

    def fake_github(_token, path):
        if path == "/user":
            return {"login": "me"}
        if "/events?" in path:
            return events
        return {"items": [], "total_count": 0}

    monkeypatch.setattr(sources, "_github", fake_github)
    monkeypatch.setattr(sources, "_github_search", lambda *_args: ([], False))
    start = datetime(2026, 10, 27, tzinfo=UTC)
    end = datetime(2026, 10, 28, tzinfo=UTC)

    collected = sources.collect_github(date(2026, 10, 27), start, end, [])
    assert len(collected) == 5
    assert {item.group_id for item in collected} == {"github:pr:o/r:42"}

    grouped = report.deduplicate(collected)
    assert len(grouped) == 1
    assert grouped[0].kind == "PullRequestGroup"
    assert grouped[0].at == "2026-10-27T09:04:00Z"
    assert grouped[0].url == "https://github.com/o/r/pull/42"
    assert "Review submitted: approved" in grouped[0].detail
    assert "Replied to a review comment" in grouped[0].detail
    assert "Commented on the PR (2 times)" in grouped[0].detail


def test_daily_update_has_markdown_and_plain_text_and_omits_excluded_items():
    included = Activity(
        "included",
        "github",
        "PullRequestGroup",
        "2026-10-27T10:00:00Z",
        "PR #1",
        summary="Reviewed the change",
        url="https://github.com/o/r/pull/1",
        issue_key="PROJ-1",
    )
    excluded = Activity(
        "excluded",
        "github",
        "Commit",
        "2026-10-27T11:00:00Z",
        "Private work",
        summary="Keep this out",
        include_in_daily_update=False,
    )
    recap = Report(
        "2026-10-27", "UTC", [included, excluded], [Issue("PROJ-1", "Review")]
    )

    report.render_report(recap, Config())

    assert recap.daily_text.startswith("# Daily update — 2026-10-27 (UTC)")
    assert "- Reviewed the change — https://github.com/o/r/pull/1" in recap.daily_text
    assert "Keep this out" not in recap.daily_text
    assert recap.daily_plain_text.startswith("Daily update — 2026-10-27 (UTC)")
    assert "- Reviewed" not in recap.daily_plain_text
    assert (
        "Reviewed the change (https://github.com/o/r/pull/1)" in recap.daily_plain_text
    )
    assert "Keep this out" not in recap.daily_plain_text


def test_deduplicates_local_and_remote_commit():
    local = Activity(
        "commit:o/r:abc", "local_git", "Commit", "2026-10-27T10:00:00Z", "A"
    )
    remote = Activity(
        "commit:o/r:abc",
        "github",
        "Commit",
        "2026-10-27T10:00:00Z",
        "A",
        url="https://github.com/o/r/commit/abc",
    )
    assert report.deduplicate([local, remote]) == [remote]


def test_credential_values_are_removed_before_ai(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-value")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-example-secret")
    assert ai_provider.redact("token test-value") == "token [redacted]"
    assert ai_provider.redact("key openai-example-secret") == "key [redacted]"
    item = Activity(
        "x",
        "codex",
        "AgentSession",
        "2026-10-27T10:00:00Z",
        "Secret",
        detail="sensitive transcript",
    )
    assert "detail" not in item.to_dict()


def _run_tui_keys(report, config, keys, *, copy=None, issues=None, output=None):
    with create_pipe_input() as input_pipe:
        tui = RecapTUI(
            report,
            config,
            copy=copy or (lambda _value: True),
            issues=issues,
            input=input_pipe,
            output=output or DummyOutput(),
        )
        tokens = re.findall(r"\x1b\[[A-Za-z~]|\x1b|.", keys, flags=re.DOTALL)

        def feed_keys():
            for token in tokens:
                try:
                    input_pipe.send_text(token)
                except ValueError:
                    return
                time.sleep(0.12 if token == "\x1b" else 0.005)

        feeder = threading.Thread(target=feed_keys, daemon=True)
        feeder.start()
        tui.run()
        feeder.join(timeout=1)
    return tui


def test_tui_uses_one_full_screen_and_restores_the_terminal(capsys):
    class RecordingOutput(DummyOutput):
        def __init__(self):
            self.alternate_screen = []

        def enter_alternate_screen(self):
            self.alternate_screen.append("entered")

        def quit_alternate_screen(self):
            self.alternate_screen.append("restored")

    output = RecordingOutput()
    tui = _run_tui_keys(Report("2026-10-27", "UTC"), Config(), "q", output=output)
    assert tui.app.full_screen is True
    assert output.alternate_screen == ["entered", "restored"]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("columns", [60, 80, 120])
def test_tui_handles_compact_and_wide_terminal_sizes(columns):
    class SizedOutput(DummyOutput):
        def get_size(self):
            return Size(rows=20, columns=columns)

    tui = _run_tui_keys(
        Report("2026-10-27", "UTC"), Config(), "q", output=SizedOutput()
    )
    assert tui.app.full_screen is True


def test_tui_handles_empty_workspaces_without_focusing_a_missing_action():
    tui = _run_tui_keys(Report("2026-10-27", "UTC"), Config(), "\r\tq")
    assert tui.view == "items"
    assert "No work items" in tui.notice


@pytest.mark.parametrize("view", ["items", "drafts"])
def test_tui_back_restores_the_parent_list_and_selection(view):
    activities = [
        Activity(
            f"{index}",
            "local_git",
            "Commit",
            f"2026-10-27T1{index}:00:00Z",
            f"Work {index}",
            summary=f"Work {index}",
        )
        for index in range(2)
    ]
    issues = [Issue("AAP-123", "First issue"), Issue("AAP-124", "Second issue")]
    for item, issue in zip(activities, issues):
        item.issue_key = issue.key
    recap = Report("2026-10-27", "UTC", activities, issues)
    report.render_report(recap, Config())

    if view == "items":
        keys = "\r\x1b[B\r\x1bq"
    else:
        keys = "\x1b[B\x1b[B\r\x1b[B\r\x1bq"
    tui = _run_tui_keys(recap, Config(), keys)
    assert tui.view == view
    assert tui.subview == ""
    assert tui.selected_index == 1


def test_tui_can_exclude_activity_from_daily_update():
    item = Activity(
        "one",
        "github",
        "Commit",
        "2026-10-27T10:00:00Z",
        "Fix",
        summary="Hidden summary",
    )
    recap = Report("2026-10-27", "UTC", [item])
    report.render_report(recap, Config())
    keys = "\r\r\x1b[B\r\x1b[B\rq"
    _run_tui_keys(recap, Config(), keys)
    assert item.include_in_daily_update is False
    assert "Hidden summary" not in recap.daily_text


def test_tui_edits_a_summary_without_printing_prompts():
    item = Activity(
        "one",
        "local_git",
        "Commit",
        "2026-10-27T10:00:00Z",
        "Old",
        summary="Old summary",
    )
    recap = Report("2026-10-27", "UTC", [item])
    report.render_report(recap, Config())
    _run_tui_keys(recap, Config(), "\r\r\r\x01\x0bUpdated summary\rq")
    assert item.summary == "Updated summary"


def test_tui_copy_is_available_from_the_main_menu():
    recap = Report(
        "2026-10-27",
        "UTC",
        [
            Activity(
                "one",
                "local_git",
                "Commit",
                "2026-10-27T10:00:00Z",
                "A",
                summary="Work",
            )
        ],
    )
    report.render_report(recap, Config())
    copied = []
    _run_tui_keys(
        recap,
        Config(),
        "\x1b[B\x1b[B\x1b[B\rq",
        copy=lambda value: copied.append(value) or True,
    )
    assert copied == [recap.daily_text]


@pytest.mark.parametrize(("approve", "should_post"), [(False, False), (True, True)])
def test_tui_requires_explicit_jira_post_approval(approve, should_post):
    class Issues:
        def __init__(self):
            self.posted = []

        def post_comment(self, key, draft):
            self.posted.append((key, draft))

    activity = Activity(
        "one",
        "local_git",
        "Commit",
        "2026-10-27T10:00:00Z",
        "Fix",
        summary="Fixed issue",
        issue_key="AAP-123",
    )
    recap = Report("2026-10-27", "UTC", [activity], [Issue("AAP-123", "Fix issue")])
    report.render_report(recap, Config())
    issues = Issues()
    keys = "\x1b[B\x1b[B\r\r\x1b[B\r"
    if approve:
        keys += "\r"
    keys += "q"
    _run_tui_keys(recap, Config(), keys, issues=issues)
    assert bool(issues.posted) is should_post
    if should_post:
        assert issues.posted == [("AAP-123", recap.comments["AAP-123"])]


def test_cli_keeps_noninteractive_output_for_pipes(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text(DEFAULT_TEXT, encoding="utf-8")

    class FakeAI:
        endpoint_url = "https://example.invalid/v1/responses"

    class FakeService:
        def __init__(self, _config):
            self.ai = FakeAI()
            self.issues = object()

        def build(self, day):
            return Report(
                day.isoformat(), "UTC", daily_text="# Daily update\n\n- Work completed"
            )

    monkeypatch.setattr(cli, "RecapService", FakeService)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["--config", str(config_path), "run", "--date", "2026-10-27"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "# Daily update\n\n- Work completed\n"
    assert "AI destination:" in captured.err


def test_ui_requires_per_ticket_approval_and_rejects_duplicate_posts(
    tmp_path, monkeypatch
):
    class FakeServer:
        def __init__(self, address, handler):
            self.server_port = 3000
            self.handler = handler

    monkeypatch.setattr(webui, "ThreadingHTTPServer", FakeServer)
    config = Config()
    server, state = make_server(config, tmp_path / "settings.toml", date(2026, 10, 27))
    item = Activity(
        "one",
        "github",
        "Commit",
        "2026-10-27T10:00:00Z",
        "Fix",
        summary="Fixed issue",
        issue_key="AAP-123",
    )
    state.report = Report(
        "2026-10-27",
        "UTC",
        [item],
        [Issue("AAP-123", "Fix")],
        comments={"AAP-123": "Fixed issue"},
    )
    state.report_id = "report-1"

    def submit(approved):
        payload = json.dumps(
            {
                "report_id": "report-1",
                "issue_key": "AAP-123",
                "text": "Fixed issue",
                "approved": approved,
            }
        ).encode()
        handler = server.handler.__new__(server.handler)
        handler.server = server
        handler.path = "/api/post"
        handler.headers = Message()
        handler.headers["X-Recap-Nonce"] = state.csrf
        handler.headers["Origin"] = "http://127.0.0.1:3000"
        handler.headers["Content-Length"] = str(len(payload))
        handler.rfile = BytesIO(payload)
        handler.wfile = BytesIO()
        handler.send_response = lambda code: setattr(handler, "status", code)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.do_POST()
        return handler.status, json.loads(handler.wfile.getvalue())

    status, _ = submit(False)
    assert status == 400
    assert not state.posted
    with patch.object(issue_provider.JiraIssueTracker, "post_comment") as post:
        assert submit(True) == (200, {"posted": "AAP-123"})
        post.assert_called_once_with("AAP-123", "Fixed issue")
        status, _ = submit(True)
        assert status == 400

    payload = json.dumps(
        {
            "report_id": "report-1",
            "item_id": "one",
            "issue_key": "",
            "include_in_daily_update": False,
        }
    ).encode()
    handler = server.handler.__new__(server.handler)
    handler.server = server
    handler.path = "/api/reassign"
    handler.headers = Message()
    handler.headers["X-Recap-Nonce"] = state.csrf
    handler.headers["Origin"] = "http://127.0.0.1:3000"
    handler.headers["Content-Length"] = str(len(payload))
    handler.rfile = BytesIO(payload)
    handler.wfile = BytesIO()
    handler.send_response = lambda code: setattr(handler, "status", code)
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None
    handler.do_POST()
    assert handler.status == 200
    assert state.report.items[0].include_in_daily_update is False
    assert "Fixed issue" not in state.report.daily_text


def test_jira_comment_formats_bullets_as_adf(monkeypatch):
    monkeypatch.setenv("JIRA_USERNAME", "someone@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-value")
    sent = {}

    def fake_http(url, **kwargs):
        sent.update(kwargs["body"])
        return {}

    monkeypatch.setattr(issue_provider, "http_json", fake_http)
    issue_provider.JiraIssueTracker(
        Config(jira_base_url="https://example.atlassian.net")
    ).post_comment("AAP-123", "Update\n\n- Fixed A\n- Tested B")
    content = sent["body"]["content"]
    assert [node["type"] for node in content] == ["paragraph", "bulletList"]
    assert len(content[1]["content"]) == 2


def test_jira_comment_urls_use_adf_link_marks(monkeypatch):
    monkeypatch.setenv("JIRA_USERNAME", "someone@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-value")
    sent = {}

    def fake_http(url, **kwargs):
        sent.update(kwargs["body"])
        return {}

    monkeypatch.setattr(issue_provider, "http_json", fake_http)
    text = (
        "See https://example.com/docs.\n\n- Review https://github.com/org/repo/pull/1"
    )
    issue_provider.JiraIssueTracker(
        Config(jira_base_url="https://example.atlassian.net")
    ).post_comment("AAP-123", text)
    paragraphs = sent["body"]["content"]
    first_inline = paragraphs[0]["content"]
    assert first_inline == [
        {"type": "text", "text": "See "},
        {
            "type": "text",
            "text": "https://example.com/docs",
            "marks": [{"type": "link", "attrs": {"href": "https://example.com/docs"}}],
        },
        {"type": "text", "text": "."},
    ]
    bullet_inline = paragraphs[1]["content"][0]["content"][0]["content"]
    assert bullet_inline[-1]["marks"][0]["type"] == "link"
    assert (
        bullet_inline[-1]["marks"][0]["attrs"]["href"]
        == "https://github.com/org/repo/pull/1"
    )


def test_service_accepts_independent_activity_ai_and_issue_providers():
    class ActivitySource:
        name = "fixture"

        def collect(self, config, day, start, end, warnings):
            return [
                Activity(
                    "one", "fixture", "Commit", "2026-10-27T10:00:00Z", "Fix AAP-123"
                )
            ]

    class AI:
        name = "fixture_ai"

        def list_models(self):
            return ["fixture-model"]

        def review(self, items, issues):
            items[0].summary = "Fixed AAP-123"
            items[0].issue_key = issues[0].key

    class Issues:
        name = "fixture_issues"

        def find_candidates(self, day, keys, warnings):
            assert keys == {"AAP-123"}
            return [Issue("AAP-123", "Fix the bug")]

        def post_comment(self, issue_key, text):
            raise AssertionError("Building a report must not post comments")

    service = report.RecapService(
        Config(timezone="UTC"),
        activity_providers=(ActivitySource(),),
        ai_provider=AI(),
        issue_tracker=Issues(),
    )
    result = service.build(date(2026, 10, 27))
    assert "Fixed AAP-123" in result.daily_text
    assert "AAP-123" in result.comments


def test_browser_assets_are_separate_package_files():
    page = (webui.STATIC_DIR / "index.html").read_text()
    assert '<script src="/assets/app.js" defer></script>' in page
    assert '<script src="/assets/theme.js"></script>' in page
    assert '<link rel="stylesheet" href="/assets/app.css">' in page
    assert 'id="openSettings"' in page
    assert 'id="themeToggle"' in page
    assert 'class="collect-actions"' in page
    assert '<select id="model"' in page
    assert 'id="customModel"' in page
    assert 'id="dailyFormat"' in page
    assert 'class="header-identity"' in page
    assert "<style>" not in page and "<script nonce=" not in page
    assert (webui.STATIC_DIR / "app.js").is_file()
    assert (webui.STATIC_DIR / "app.css").is_file()
    assert (webui.STATIC_DIR / "theme.js").is_file()
    theme = (webui.STATIC_DIR / "theme.js").read_text()
    assert "prefers-color-scheme: dark" in theme
    assert "recap-theme" in theme
    app = (webui.STATIC_DIR / "app.js").read_text()
    assert "window.confirm" not in app
    assert "approval-preview" in app
    assert "approved: true" in app
    assert "None — exclude from daily update" in app


def test_openai_responses_request_and_raw_output_parsing(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-example-token")
    monkeypatch.setenv("RECAP_AI_API_KEY", "legacy-example-token")
    sent = {}

    def fake_http(url, **kwargs):
        sent.update(url=url, **kwargs)
        return {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": '{"items":[]}'}],
                },
            ],
        }

    monkeypatch.setattr(ai_provider, "http_json", fake_http)
    config = Config(ai_model="example-model", ai_max_output_tokens=2048)
    provider = ai_provider.create_ai_provider(config)
    result = provider._complete([{"role": "user", "content": "Summarize"}])
    assert result == '{"items":[]}'
    assert sent["url"] == "https://api.openai.com/v1/responses"
    assert sent["body"] == {
        "model": "example-model",
        "input": [{"role": "user", "content": "Summarize"}],
        "max_output_tokens": 2048,
        "service_tier": "default",
        "reasoning": {"effort": "low"},
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "recap_report",
                "strict": True,
                "schema": ai_provider.REPORT_SCHEMA,
            }
        },
    }
    assert sent["token"] == "openai-example-token"


def test_ai_review_sends_all_activity_and_issues_in_one_request(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-example-token")
    items = [
        Activity(
            str(index), "github", "Commit", "2026-10-27T10:00:00Z", f"Work item {index}"
        )
        for index in range(21)
    ]
    issues = [
        Issue(f"PROJ-{index}", f"Candidate ticket {index}", "In Progress")
        for index in range(11)
    ]
    sent = []

    def fake_http(url, **kwargs):
        sent.append((url, kwargs))
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "items": [
                                        {
                                            "id": item.id,
                                            "summary": f"Completed work item {item.id}",
                                            "issue_key": None,
                                        }
                                        for item in items
                                    ]
                                }
                            ),
                        }
                    ],
                }
            ],
        }

    monkeypatch.setattr(ai_provider, "http_json", fake_http)
    provider = ai_provider.OpenAIResponsesProvider(Config(ai_model="example-model"))
    provider.review(items, issues)

    assert len(sent) == 1
    request = sent[0][1]["body"]
    payload = json.loads(request["input"][1]["content"])
    assert len(payload["activities"]) == 21
    assert len(payload["jira_issues"]) == 11
    assert payload["jira_issues"][-1]["key"] == "PROJ-10"
    assert all(item.summary.startswith("Completed work item") for item in items)


def test_ai_review_preserves_explicit_jira_key_and_rejects_unknown_keys(monkeypatch):
    item = Activity("one", "github", "Commit", "2026-10-27T10:00:00Z", "Fix PROJ-1")
    provider = ai_provider.OpenAIResponsesProvider(Config(ai_model="example-model"))
    provider._complete = lambda _messages: json.dumps(
        {
            "items": [
                {
                    "id": "one",
                    "summary": "Fixed the requested change",
                    "issue_key": "PROJ-2",
                }
            ]
        }
    )
    provider.review(
        [item], [Issue("PROJ-1", "Fix issue"), Issue("PROJ-2", "Other issue")]
    )
    assert item.issue_key == "PROJ-1"

    unmatched = Activity("two", "github", "Commit", "2026-10-27T10:00:00Z", "Work")
    provider._complete = lambda _messages: json.dumps(
        {
            "items": [
                {
                    "id": "two",
                    "summary": "Completed the requested work",
                    "issue_key": "OTHER-9",
                }
            ]
        }
    )
    with pytest.raises(sources.SourceError, match="unlisted Jira key"):
        provider.review([unmatched], [Issue("PROJ-1", "Fix issue")])
    assert unmatched.summary == ""


def test_ai_review_accepts_summaries_over_fourteen_words():
    item = Activity("one", "github", "Commit", "2026-10-27T10:00:00Z", "Work")
    provider = ai_provider.OpenAIResponsesProvider(Config(ai_model="example-model"))
    summary = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen"

    def answer(messages):
        assert "at most 14 whitespace-separated words" in messages[0]["content"]
        return json.dumps(
            {"items": [{"id": "one", "summary": summary, "issue_key": None}]}
        )

    provider._complete = answer
    provider.review([item], [])
    assert item.summary == summary


def test_ai_review_does_not_call_provider_for_empty_activity():
    provider = ai_provider.OpenAIResponsesProvider(Config(ai_model="example-model"))
    provider._complete = lambda _messages: pytest.fail(
        "empty report must not call the AI provider"
    )
    provider.review([], [Issue("PROJ-1", "Fix issue")])


def test_jira_candidate_search_paginates_all_pages(monkeypatch):
    monkeypatch.setenv("JIRA_USERNAME", "user@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-example-token")
    requests = []

    def fake_http(_url, **kwargs):
        token = kwargs["body"].get("nextPageToken", "")
        page = int(token) if token else 0
        requests.append(page)
        result = {
            "issues": [{"key": f"PROJ-{page}", "fields": {"summary": f"Issue {page}"}}]
        }
        if page < 20:
            result["nextPageToken"] = str(page + 1)
        return result

    monkeypatch.setattr(issue_provider, "http_json", fake_http)
    warnings = []
    result = issue_provider.JiraIssueTracker(
        Config(jira_base_url="https://example.atlassian.net")
    ).find_candidates(date(2026, 10, 27), set(), warnings)

    assert len(requests) == 21
    assert len(result) == 21
    assert warnings == []


def test_legacy_openai_config_uses_responses_and_local_chat_keeps_chat_endpoint(
    monkeypatch,
):
    assert isinstance(
        ai_provider.create_ai_provider(Config(ai_provider="openai_compatible")),
        ai_provider.OpenAIResponsesProvider,
    )
    monkeypatch.delenv("RECAP_AI_API_KEY", raising=False)
    sent = {}

    def fake_http(url, **kwargs):
        sent.update(url=url, **kwargs)
        return {"choices": [{"message": {"content": "ready"}}]}

    monkeypatch.setattr(ai_provider, "http_json", fake_http)
    config = Config(
        ai_provider="chat_completions",
        ai_base_url="http://localhost:11434/v1",
        ai_model="local-model",
    )
    provider = ai_provider.create_ai_provider(config)
    assert provider._complete([{"role": "user", "content": "Hi"}]) == "ready"
    assert sent["url"] == "http://localhost:11434/v1/chat/completions"
    assert sent["token"] == ""
    assert "reasoning_effort" not in sent["body"]
    assert "service_tier" not in sent["body"]
    assert sent["body"]["max_tokens"] == config.ai_max_output_tokens


def test_openai_key_is_not_sent_to_other_remote_hosts(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-example-token")
    monkeypatch.delenv("RECAP_AI_API_KEY", raising=False)
    provider = ai_provider.create_ai_provider(
        Config(ai_provider="chat_completions", ai_base_url="https://models.example/v1")
    )
    with pytest.raises(sources.SourceError, match="RECAP_AI_API_KEY"):
        provider._token()


def test_openai_chat_completions_uses_low_reasoning_and_default_service_tier(
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-example-token")
    sent = {}

    def fake_http(url, **kwargs):
        sent.update(url=url, **kwargs)
        return {"choices": [{"message": {"content": '{"items": []}'}}]}

    monkeypatch.setattr(ai_provider, "http_json", fake_http)
    provider = ai_provider.ChatCompletionsProvider(Config(ai_model="example-model"))
    assert (
        provider._complete([{"role": "user", "content": "Summarize"}])
        == '{"items": []}'
    )
    assert sent["url"] == "https://api.openai.com/v1/chat/completions"
    assert sent["body"] == {
        "model": "example-model",
        "messages": [{"role": "user", "content": "Summarize"}],
        "max_completion_tokens": 4096,
        "reasoning_effort": "low",
        "service_tier": "default",
    }


def test_model_listing_keeps_all_returned_model_ids(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-example-token")
    ids = [f"model-{index}" for index in range(20)]
    monkeypatch.setattr(
        ai_provider,
        "http_json",
        lambda *_args, **_kwargs: {"data": [{"id": model} for model in ids]},
    )
    provider = ai_provider.OpenAIResponsesProvider(Config(ai_model="example-model"))
    assert provider.list_models() == ids


def test_capture_demo_builds_a_fresh_synthetic_report():
    config = Config(timezone="UTC", ai_model=demo.DEMO_MODEL)
    service = demo.DemoService(config)
    result = service.build(demo.DEMO_DAY)

    assert {issue.key for issue in result.issues} == {"MOCK-123", "MOCK-124"}
    assert {item.issue_key for item in result.items if item.issue_key} == {
        "MOCK-123",
        "MOCK-124",
    }
    assert any(
        not item.issue_key and item.include_in_daily_update for item in result.items
    )
    assert any(not item.include_in_daily_update for item in result.items)
    assert set(result.comments) == {"MOCK-123", "MOCK-124"}
    assert "MOCK-123" in result.comments["MOCK-123"]


def test_capture_demo_web_server_uses_simulated_services(tmp_path, monkeypatch):
    class FakeServer:
        def __init__(self, _address, handler):
            self.server_port = 3000
            self.handler = handler

    monkeypatch.setattr(webui, "ThreadingHTTPServer", FakeServer)
    config = Config(timezone="UTC", ai_model=demo.DEMO_MODEL)
    server, state = webui.make_server(
        config,
        tmp_path / "demo-config.toml",
        demo.DEMO_DAY,
        service_factory=demo.DemoService,
        demo_mode=True,
    )

    get_handler = server.handler.__new__(server.handler)
    get_handler.server = server
    get_handler.path = "/"
    get_handler.send_response = lambda _code: None
    get_handler.send_header = lambda *_args: None
    get_handler.end_headers = lambda: None
    get_handler.wfile = BytesIO()
    get_handler.do_GET()
    page = get_handler.wfile.getvalue().decode()
    assert 'name="recap-demo" content="true"' in page
    assert "__RECAP_DEMO__" not in page

    report_value = state.run_service.build(demo.DEMO_DAY)
    state.report = report_value
    state.report_id = "demo-report"
    payload = json.dumps(
        {
            "report_id": state.report_id,
            "issue_key": "MOCK-123",
            "text": report_value.comments["MOCK-123"],
            "approved": True,
        }
    ).encode()
    post_handler = server.handler.__new__(server.handler)
    post_handler.server = server
    post_handler.path = "/api/post"
    post_handler.headers = Message()
    post_handler.headers["X-Recap-Nonce"] = state.csrf
    post_handler.headers["Origin"] = "http://127.0.0.1:3000"
    post_handler.headers["Content-Length"] = str(len(payload))
    post_handler.rfile = BytesIO(payload)
    post_handler.wfile = BytesIO()
    post_handler.send_response = lambda code: setattr(post_handler, "status", code)
    post_handler.send_header = lambda *_args: None
    post_handler.end_headers = lambda: None
    post_handler.do_POST()

    assert post_handler.status == 200
    assert json.loads(post_handler.wfile.getvalue()) == {
        "posted": "MOCK-123",
        "simulated": True,
    }
    assert state.run_service.issues.posted_comments == [
        ("MOCK-123", report_value.comments["MOCK-123"])
    ]


def test_capture_demo_tui_simulates_copy_and_jira_actions():
    config = Config(timezone="UTC", ai_model=demo.DEMO_MODEL)
    service = demo.DemoService(config)
    copied = []
    tui = RecapTUI(
        service.build(demo.DEMO_DAY),
        config,
        copy=lambda value: copied.append(value) or True,
        issues=service.issues,
        demo_mode=True,
    )

    tui._copy("synthetic clipboard text")
    assert copied == ["synthetic clipboard text"]
    assert "copy simulated" in tui.notice.lower()
    tui.current_draft_key = "MOCK-123"
    tui._post_draft()
    assert service.issues.posted_comments[0][0] == "MOCK-123"
    assert "simulated jira comment" in tui.notice.lower()


def test_default_config_path_prefers_config_in_launch_directory(tmp_path, monkeypatch):
    local_config = tmp_path / "config.toml"
    local_config.write_text('repo_roots = []\ntimezone = "UTC"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    fallback = tmp_path / "user-config" / "config.toml"
    monkeypatch.setattr(config_module, "config_path", lambda: fallback)

    assert default_config_path() == local_config


def test_default_config_path_uses_user_config_when_local_file_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    fallback = tmp_path / "user-config" / "config.toml"
    monkeypatch.setattr(config_module, "config_path", lambda: fallback)

    assert default_config_path() == fallback


def test_cli_run_uses_local_config_without_config_argument(
    tmp_path, monkeypatch, capsys
):
    local_config = tmp_path / "config.toml"
    root = tmp_path / "chosen-repository-root"
    local_config.write_text(
        f'repo_roots = ["{root.as_posix()}"]\ntimezone = "UTC"\n',
        encoding="utf-8",
    )
    captured_configs = []

    class FakeAI:
        endpoint_url = "mock endpoint"

    class FakeService:
        def __init__(self, config):
            captured_configs.append(config)
            self.ai = FakeAI()

        def build(self, day):
            return Report(day.isoformat(), "UTC", daily_text="Daily update")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "RecapService", FakeService)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert cli.main(["run", "--date", "2026-01-15"]) == 0
    assert captured_configs[0].repo_roots == [root]
    assert "Daily update" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["--help"],
            [
                "Prepare an end-of-day engineering activity update",
                "run",
                "ui",
                "models",
                "repos",
                "config",
            ],
        ),
        (
            ["run", "--help"],
            [
                "full-screen terminal workspace",
                "--date",
                "--days-ago",
                "--tz",
                "--model",
                "--repo-root",
                "before 'run'",
            ],
        ),
        (["ui", "--help"], ["loopback-only browser UI", "--date", "--days-ago"]),
        (
            ["models", "--help"],
            ["available model IDs", "configured AI endpoint", "before 'models'"],
        ),
        (
            ["repos", "--help"],
            [
                "Git repositories and worktrees",
                "multiple directories",
                "before 'repos'",
            ],
        ),
        (
            ["config", "--help"],
            ["print the file Recap will use", "init", "path", "./config.toml"],
        ),
        (
            ["config", "init", "--help"],
            ["Create a non-secret config file", "already exists", "--config PATH"],
        ),
        (
            ["config", "path", "--help"],
            ["Print the config file", "./config.toml", "OS user config path"],
        ),
    ],
)
def test_cli_help_describes_commands_and_options(argv, expected, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)

    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    for phrase in expected:
        assert phrase in output


def test_pyproject_declares_runtime_and_development_dependencies():
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    metadata = tomllib.loads(project_file.read_text(encoding="utf-8"))

    assert "tzlocal>=5,<6" in metadata["project"]["dependencies"]
    assert "prompt_toolkit>=3.0.52,<4" in metadata["project"]["dependencies"]
    dev_dependencies = metadata["project"]["optional-dependencies"]["dev"]
    assert any(dependency.startswith("pytest") for dependency in dev_dependencies)
    assert any(dependency.startswith("ruff") for dependency in dev_dependencies)

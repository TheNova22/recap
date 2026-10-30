"""Full-screen, keyboard-driven terminal workflow for reviewing a Recap report."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from .config import Config
from .issue_provider import IssueTracker, create_issue_tracker
from .model import Activity, Report
from .report import render_report
from .sources import SourceError

_STYLE = Style.from_dict(
    {
        "brand": "bold fg:#d7ea73",
        "header": "fg:#dbe5df",
        "header.meta": "fg:#8aa39a",
        "pane.border": "fg:#48645a",
        "pane.title": "bold fg:#d7ea73",
        "muted": "fg:#91a39a",
        "choice": "fg:#dbe5df",
        "choice.current": "bold fg:#18302d bg:#d7ea73",
        "choice.inactive": "bold fg:#d7ea73",
        "detail.title": "bold fg:#d7ea73",
        "detail.label": "fg:#8aa39a",
        "detail.text": "fg:#e4ebe6",
        "detail.url": "underline fg:#86bcb0",
        "footer": "fg:#9fb2a8",
        "notice": "fg:#b6d9c7",
        "error": "bold fg:#ff9b8f",
        "edit-area": "fg:#e4ebe6",
        "edit-area focused": "fg:#e4ebe6",
    }
)


class RecapTUI:
    """One full-screen application; all navigation updates the current frame."""

    def __init__(
        self,
        report: Report,
        config: Config,
        *,
        copy: Any,
        issues: IssueTracker | None = None,
        input: Any = None,
        output: Any = None,
        demo_mode: bool = False,
    ):
        self.report = report
        self.config = config
        self.copy = copy
        self.issues = issues if issues is not None else create_issue_tracker(config)
        self.demo_mode = demo_mode
        self.input = input
        self.output = output
        self.posted: set[str] = set()

        self.view = "home"
        self.subview = ""
        self.focus = "left"
        self.selected_index = 0
        self.action_index = 0
        self.current_item_index: int | None = None
        self.current_draft_key = ""
        self.notice = (
            "Demo mode · synthetic data and simulated actions." if demo_mode else ""
        )
        self.notice_is_error = False
        self.editing = False
        self.app: Application[Any] | None = None

        self.left_window = Window(
            FormattedTextControl(self._left_content, focusable=True),
            wrap_lines=True,
            always_hide_cursor=True,
            style="class:list-pane",
        )
        self.right_window = Window(
            FormattedTextControl(self._right_content, focusable=True),
            wrap_lines=True,
            always_hide_cursor=True,
            style="class:detail-pane",
        )
        self.edit_area = TextArea(
            text="",
            multiline=False,
            wrap_lines=False,
            scrollbar=False,
            style="class:edit-area",
        )
        self._build_application()

    def _build_application(self) -> None:
        right_body = HSplit(
            [
                Window(
                    FormattedTextControl(self._right_title),
                    height=1,
                    style="class:pane.title",
                ),
                ConditionalContainer(
                    self.right_window, filter=Condition(lambda: not self.editing)
                ),
                ConditionalContainer(
                    self.edit_area, filter=Condition(lambda: self.editing)
                ),
            ]
        )
        left_body = HSplit(
            [
                Window(
                    FormattedTextControl(self._left_title),
                    height=1,
                    style="class:pane.title",
                ),
                self.left_window,
            ]
        )
        body = VSplit(
            [
                Frame(left_body, width=Dimension(weight=2, min=22), style="class:pane"),
                Frame(
                    right_body, width=Dimension(weight=3, min=30), style="class:pane"
                ),
            ],
            padding=1,
            padding_char=" ",
        )
        root = HSplit(
            [
                Window(
                    FormattedTextControl(self._header),
                    height=2,
                    wrap_lines=True,
                    style="class:header",
                ),
                body,
                Window(
                    FormattedTextControl(self._footer),
                    height=3,
                    wrap_lines=True,
                    style="class:footer",
                ),
            ]
        )
        bindings = KeyBindings()
        editing = Condition(lambda: self.editing)
        browsing = ~editing

        @bindings.add("up", filter=browsing, eager=True)
        def move_up(event) -> None:
            self._move(-1)

        @bindings.add("down", filter=browsing, eager=True)
        def move_down(event) -> None:
            self._move(1)

        @bindings.add("left", filter=browsing, eager=True)
        def focus_left(event) -> None:
            self._set_focus("left")

        @bindings.add("right", filter=browsing, eager=True)
        def focus_right(event) -> None:
            self._focus_actions()

        @bindings.add("tab", filter=browsing, eager=True)
        def next_pane(event) -> None:
            if self.view in ("items", "drafts"):
                self._focus_actions() if self.focus == "left" else self._set_focus(
                    "left"
                )

        @bindings.add("enter", filter=browsing, eager=True)
        def activate(event) -> None:
            self._activate()

        @bindings.add("c-m", filter=browsing, eager=True)
        def activate_return(event) -> None:
            self._activate()

        @bindings.add("escape", filter=browsing, eager=True)
        def back(event) -> None:
            self._back()

        @bindings.add("backspace", filter=browsing, eager=True)
        def backspace(event) -> None:
            self._back()

        @bindings.add("pageup", filter=browsing, eager=True)
        def page_up(event) -> None:
            self._scroll(-8)

        @bindings.add("pagedown", filter=browsing, eager=True)
        def page_down(event) -> None:
            self._scroll(8)

        @bindings.add("q", filter=browsing, eager=True)
        def quit_app(event) -> None:
            event.app.exit(result="quit")

        @bindings.add("c-c")
        def interrupt(event) -> None:
            if self.editing:
                self._cancel_edit()
            else:
                event.app.exit(result="quit")

        @bindings.add("enter", filter=editing, eager=True)
        def save_edit_on_enter(event) -> None:
            self._save_edit()

        @bindings.add("c-m", filter=editing, eager=True)
        def save_edit_on_return(event) -> None:
            self._save_edit()

        @bindings.add("c-s", filter=editing, eager=True)
        def save_edit(event) -> None:
            self._save_edit()

        @bindings.add("escape", filter=editing, eager=True)
        def cancel_edit(event) -> None:
            self._cancel_edit()

        self.app = Application(
            layout=Layout(root, focused_element=self.left_window),
            key_bindings=bindings,
            style=_STYLE,
            full_screen=True,
            mouse_support=False,
            input=self.input,
            output=self.output,
        )

    def _left_options(self) -> list[tuple[str, str, str]]:
        if self.view == "home":
            return [
                ("items", "Review work items", f"{len(self.report.items)} collected"),
                (
                    "daily",
                    "Preview daily update",
                    "Review and copy the Markdown update",
                ),
                ("drafts", "Review Jira drafts", f"{len(self.report.comments)} ready"),
                ("copy", "Copy daily update", "Copy the current update to clipboard"),
                ("quit", "Exit Recap", "Close the full-screen view"),
            ]
        if self.view == "items":
            if not self.report.items:
                return [
                    (
                        "empty",
                        "No work items found",
                        "Try another date or repository folder",
                    )
                ]
            return [
                (
                    str(index),
                    item.summary or item.title,
                    self._activity_destination(item),
                )
                for index, item in enumerate(self.report.items)
            ]
        if self.view == "daily":
            options = [("copy", "Copy daily update", "Copy this preview to clipboard")]
            if self.report.comments:
                options.append(
                    (
                        "drafts",
                        "Review Jira drafts",
                        f"{len(self.report.comments)} ready",
                    )
                )
            return options
        if self.view == "drafts":
            if not self.report.comments:
                return [
                    (
                        "empty",
                        "No Jira drafts",
                        "Match work items to Jira issues to create drafts",
                    )
                ]
            return [
                (
                    key,
                    f"{key}  {self._issue_title(key)}",
                    "Posted" if key in self.posted else "Ready for review",
                )
                for key in self.report.comments
            ]
        return []

    def _right_options(self) -> list[tuple[str, str, str]]:
        if self.view == "items" and self.subview == "actions":
            return [
                ("edit", "Edit summary", "Change the wording in this report"),
                ("match", "Change Jira match", "Assign, keep daily-only, or exclude"),
            ]
        if self.view == "items" and self.subview == "match":
            options = [
                (
                    "daily",
                    "Daily update only",
                    "Keep this activity without a Jira ticket",
                ),
                (
                    "exclude",
                    "None — exclude from daily update",
                    "Remove this activity from the update",
                ),
            ]
            options.extend(
                (issue.key, issue.title, "Jira issue") for issue in self.report.issues
            )
            options.append(
                ("back", "Back to item actions", "Return to the previous choices")
            )
            return options
        if self.view == "drafts" and self.subview == "actions":
            options = [("copy", "Copy draft", "Copy the reviewed text to clipboard")]
            if self.current_draft_key not in self.posted:
                options.append(
                    (
                        "approve",
                        f"Post to {self.current_draft_key}",
                        "Review the exact comment before posting",
                    )
                )
            return options
        if self.view == "drafts" and self.subview == "approval":
            return [
                (
                    "approve",
                    f"Approve and post to {self.current_draft_key}",
                    "This sends the displayed comment to Jira",
                ),
                ("back", "Back to draft actions", "Return without posting"),
            ]
        return []

    def _activity_destination(self, item: Activity) -> str:
        if item.include_in_daily_update is False:
            return "Excluded from daily update"
        return item.issue_key or "Daily update only"

    def _selected_item(self) -> Activity | None:
        if self.current_item_index is not None and 0 <= self.current_item_index < len(
            self.report.items
        ):
            return self.report.items[self.current_item_index]
        if self.view == "items" and 0 <= self.selected_index < len(self.report.items):
            return self.report.items[self.selected_index]
        return None

    def _selected_draft_key(self) -> str:
        if self.current_draft_key in self.report.comments:
            return self.current_draft_key
        options = self._left_options()
        if (
            self.view == "drafts"
            and options
            and 0 <= self.selected_index < len(options)
        ):
            return options[self.selected_index][0]
        return ""

    def _render_options(
        self,
        options: Sequence[tuple[str, str, str]],
        selected: int,
        focused: bool,
    ) -> FormattedText:
        tokens: list[tuple[str, str]] = []
        for index, (_value, label, detail) in enumerate(options):
            is_selected = index == selected
            if is_selected and focused:
                tokens.append(("class:choice.current", f"  › {label}\n"))
                tokens.append(("class:choice.current", f"    {detail}\n"))
            else:
                tokens.append(("class:choice", f"    {label}\n"))
                tokens.append(("class:muted", f"      {detail}\n"))
        return FormattedText(tokens)

    def _left_title(self) -> FormattedText:
        titles = {
            "home": "RECAP  /  MENU",
            "items": "WORK ITEMS",
            "daily": "DAILY UPDATE",
            "drafts": "JIRA DRAFTS",
        }
        active = self.focus == "left"
        style = "class:pane.title" if active else "class:muted"
        return FormattedText([(style, titles.get(self.view, "RECAP"))])

    def _right_title(self) -> FormattedText:
        titles = {
            "home": "TODAY AT A GLANCE",
            "items": "WORK ITEM DETAILS",
            "daily": "UPDATE PREVIEW",
            "drafts": "JIRA COMMENT",
        }
        active = self.focus == "right"
        style = "class:pane.title" if active else "class:muted"
        return FormattedText([(style, titles.get(self.view, "DETAILS"))])

    def _header(self) -> FormattedText:
        title = f"  recap    {self.report.date}  /  {self.report.timezone}"
        counts = f"{len(self.report.items)} activities    {len(self.report.comments)} Jira drafts"
        return FormattedText(
            [
                ("class:brand", "  recap"),
                (
                    "class:header.meta",
                    f"    {self.report.date}  ·  {self.report.timezone}",
                ),
                ("class:header", f"    {counts}\n"),
                (
                    "class:pane.border",
                    "  " + "─" * max(12, min(80, len(title) + len(counts))),
                ),
            ]
        )

    def _footer(self) -> FormattedText:
        width = self.app.output.get_size().columns if self.app is not None else 120
        compact = width < 88
        if self.editing:
            hints = (
                "  Enter / Ctrl-S Save   Esc Cancel"
                if compact
                else "  Enter / Ctrl-S Save     Esc Cancel     q stays in editor"
            )
        elif self.view in ("items", "drafts"):
            hints = (
                "  ↑↓ Move  Enter Select  Tab Pane  Esc Back  q Exit"
                if compact
                else "  ↑↓ Move     Enter Details / Select     Tab Switch pane     Esc Back     q Exit"
            )
        elif self.view == "home":
            hints = (
                "  ↑↓ Move  Enter Open  q Exit"
                if compact
                else "  ↑↓ Move     Enter Open     q Exit"
            )
        else:
            hints = (
                "  ↑↓ Move  Enter Select  PgUp/Dn Scroll  Esc Back  q Exit"
                if compact
                else "  ↑↓ Move     Enter Select     PgUp/PgDn Scroll     Esc Back     q Exit"
            )
        notice_style = "class:error" if self.notice_is_error else "class:notice"
        notice = (
            self.notice
            or "  Recap keeps activity and drafts in this process until you exit."
        )
        return FormattedText(
            [("class:footer", hints + "\n"), (notice_style, "  " + notice)]
        )

    def _left_content(self) -> FormattedText:
        options = self._left_options()
        tokens: list[tuple[str, str]] = []
        if self.view == "items" and not self.report.items:
            tokens.extend(
                [
                    ("class:muted", "\n  No work items are available.\n"),
                    (
                        "class:detail.text",
                        "  Try another date or add a repository folder in Settings.\n",
                    ),
                ]
            )
            return FormattedText(tokens)
        if self.view == "drafts" and not self.report.comments:
            tokens.extend(
                [
                    ("class:muted", "\n  No ticket drafts are ready.\n"),
                    (
                        "class:detail.text",
                        "  Match an activity to a Jira issue to create one.\n",
                    ),
                ]
            )
            return FormattedText(tokens)
        if self.view == "items":
            for index, item in enumerate(self.report.items):
                title = item.summary or item.title
                title = title.replace("\n", " ").strip()
                if len(title) > 67:
                    title = title[:64].rstrip() + "…"
                marker = " › " if index == self.selected_index else "   "
                choice_style = (
                    "class:choice.current"
                    if index == self.selected_index and self.focus == "left"
                    else "class:choice.inactive"
                    if index == self.selected_index
                    else "class:choice"
                )
                detail_style = (
                    "class:choice.current"
                    if index == self.selected_index and self.focus == "left"
                    else "class:muted"
                )
                tokens.append((choice_style, f"{marker}{title}\n"))
                destination = self._activity_destination(item)
                tokens.append((detail_style, f"     {item.source}  ·  {destination}\n"))
            return FormattedText(tokens)
        return self._render_options(options, self.selected_index, self.focus == "left")

    def _right_content(self) -> FormattedText:
        if self.view == "home":
            options = self._left_options()
            value, _label, _detail = (
                options[self.selected_index] if options else ("", "", "")
            )
            descriptions = {
                "items": "Review collected work, edit summaries, and connect activity to Jira issues.",
                "daily": "Read the complete daily update before copying it to your clipboard.",
                "drafts": "Review each Jira comment and approve posting one ticket at a time.",
                "copy": "Copy the current Markdown update without leaving the menu.",
                "quit": "Close Recap and return to the terminal exactly as it was.",
            }
            return FormattedText(
                [
                    ("class:detail.title", "\n  End-of-day review\n\n"),
                    (
                        "class:detail.text",
                        "  One screen for the work collected today.\n\n",
                    ),
                    ("class:detail.label", "  SELECTED ACTION\n"),
                    ("class:detail.text", f"  {descriptions.get(value, '')}\n\n"),
                    (
                        "class:muted",
                        "  Work items include local Git, GitHub, and enabled agent sessions.\n",
                    ),
                ]
            )
        if self.view == "items":
            return self._item_detail()
        if self.view == "daily":
            return FormattedText(
                [
                    (
                        "class:detail.text",
                        "\n"
                        + (self.report.daily_text or "No daily update is available."),
                    )
                ]
            )
        if self.view == "drafts":
            return self._draft_detail()
        return FormattedText([])

    def _item_detail(self) -> FormattedText:
        item = self._selected_item()
        if item is None:
            return FormattedText(
                [("class:muted", "\n  Select a work item to see its details.\n")]
            )
        tokens: list[tuple[str, str]] = [
            ("class:detail.title", "\n  " + (item.summary or item.title) + "\n\n"),
            ("class:detail.label", "  SOURCE     "),
            ("class:detail.text", f"{item.source}   {item.at or 'time unavailable'}\n"),
            ("class:detail.label", "  JIRA       "),
            ("class:detail.text", f"{self._activity_destination(item)}\n"),
        ]
        if item.url:
            tokens.extend(
                [
                    ("class:detail.label", "  LINK       "),
                    ("class:detail.url", item.url + "\n"),
                ]
            )
        if item.detail:
            tokens.extend(
                [
                    ("class:detail.label", "\n  ACTIVITY\n"),
                    ("class:detail.text", "  " + item.detail.strip() + "\n"),
                ]
            )
        if self.subview == "edit":
            tokens.extend(
                [
                    ("class:detail.label", "\n  EDIT SUMMARY\n"),
                    (
                        "class:muted",
                        "  Update the text below. Enter saves; Esc cancels.\n",
                    ),
                ]
            )
        elif self.subview == "match":
            tokens.extend(
                [
                    ("class:detail.label", "\n  SET DESTINATION\n"),
                    (
                        "class:muted",
                        "  Choose a ticket, keep it daily-only, or exclude it.\n",
                    ),
                ]
            )
            tokens.extend(
                self._render_options(
                    self._right_options(), self.action_index, self.focus == "right"
                )
            )
        elif self.subview == "actions":
            tokens.extend([("class:detail.label", "\n  ACTIONS\n")])
            tokens.extend(
                self._render_options(
                    self._right_options(), self.action_index, self.focus == "right"
                )
            )
        else:
            tokens.append(
                ("class:muted", "\n  Enter or Tab to see available actions.\n")
            )
        return FormattedText(tokens)

    def _draft_detail(self) -> FormattedText:
        key = self._selected_draft_key()
        if not key:
            return FormattedText(
                [("class:muted", "\n  Select a Jira draft to preview its comment.\n")]
            )
        issue_title = self._issue_title(key)
        draft = self.report.comments.get(key, "")
        tokens: list[tuple[str, str]] = [
            ("class:detail.title", f"\n  {key}\n"),
            ("class:detail.label", f"  {issue_title}\n\n"),
            ("class:detail.text", "  " + draft.replace("\n", "\n  ") + "\n"),
        ]
        if key in self.posted:
            tokens.append(("class:notice", "\n  Posted in this session.\n"))
        elif self.subview == "approval":
            tokens.extend(
                [
                    ("class:error", "\n  APPROVAL REQUIRED\n"),
                    (
                        "class:detail.text",
                        "  This sends the exact comment above to Jira.\n",
                    ),
                ]
            )
            tokens.extend(
                self._render_options(
                    self._right_options(), self.action_index, self.focus == "right"
                )
            )
        elif self.subview == "actions":
            tokens.extend([("class:detail.label", "\n  ACTIONS\n")])
            tokens.extend(
                self._render_options(
                    self._right_options(), self.action_index, self.focus == "right"
                )
            )
        else:
            tokens.append(
                ("class:muted", "\n  Enter or Tab to see available actions.\n")
            )
        return FormattedText(tokens)

    def _issue_title(self, key: str) -> str:
        return next(
            (issue.title for issue in self.report.issues if issue.key == key), key
        )

    def _current_options(self) -> list[tuple[str, str, str]]:
        return self._right_options() if self.focus == "right" else self._left_options()

    def _move(self, amount: int) -> None:
        options = self._current_options()
        if not options:
            return
        selected = self.action_index if self.focus == "right" else self.selected_index
        selected = max(0, min(len(options) - 1, selected + amount))
        if self.focus == "right":
            self.action_index = selected
        else:
            self.selected_index = selected
            if self.view == "items" and self.report.items:
                self.current_item_index = selected
            elif self.view == "drafts":
                options = self._left_options()
                if options and options[selected][0] != "empty":
                    self.current_draft_key = options[selected][0]
            self.left_window.vertical_scroll = max(0, selected * 2 - 5)
        self._clear_notice()
        self._invalidate()

    def _scroll(self, amount: int) -> None:
        window = (
            self.right_window
            if self.view == "daily" or self.focus == "right"
            else self.left_window
        )
        window.vertical_scroll = max(0, window.vertical_scroll + amount)
        self._invalidate()

    def _focus_actions(self) -> None:
        if self.view not in ("items", "drafts"):
            return
        if self.view == "items" and not self.report.items:
            self._set_notice("No work items are available.")
            return
        if self.view == "drafts" and not self.report.comments:
            self._set_notice("No Jira drafts are available.")
            return
        if not self.subview:
            if self.view == "items" and self.report.items:
                self.current_item_index = self.selected_index
                self.subview = "actions"
            elif self.view == "drafts" and self.report.comments:
                options = self._left_options()
                if options:
                    self.current_draft_key = options[self.selected_index][0]
                    self.subview = "actions"
        self._set_focus("right")

    def _set_focus(self, target: str) -> None:
        if target == "right" and self.view not in ("items", "drafts"):
            return
        if target == "right" and not self.subview:
            self._focus_actions()
            return
        self.focus = target
        if self.app is not None:
            self.app.layout.focus(
                self.right_window if target == "right" else self.left_window
            )
        self._invalidate()

    def _activate(self) -> None:
        options = self._current_options()
        index = self.action_index if self.focus == "right" else self.selected_index
        if not options or index >= len(options):
            return
        value = options[index][0]

        if self.view == "home":
            if value == "items":
                self._open_view("items")
            elif value == "daily":
                self._open_view("daily")
            elif value == "drafts":
                self._open_view("drafts")
            elif value == "copy":
                self._copy(self.report.daily_text)
            elif value == "quit":
                self.app.exit(result="quit")
            return

        if self.view == "items":
            if self.focus == "left":
                if value != "empty":
                    self.current_item_index = int(value)
                    self.subview = "actions"
                    self.action_index = 0
                    self._set_focus("right")
                return
            if self.subview == "actions":
                if value == "edit":
                    self._begin_edit()
                elif value == "match":
                    self.subview = "match"
                    self.action_index = 0
            elif self.subview == "match":
                if value == "back":
                    self.subview = "actions"
                    self.action_index = 0
                else:
                    self._apply_match(value)
            self._invalidate()
            return

        if self.view == "daily":
            if value == "copy":
                self._copy(self.report.daily_text)
            elif value == "drafts":
                self._open_view("drafts")
            return

        if self.view == "drafts":
            if self.focus == "left":
                if value != "empty":
                    self.current_draft_key = value
                    self.subview = "actions"
                    self.action_index = 0
                    self._set_focus("right")
                return
            if self.subview == "actions":
                if value == "copy":
                    self._copy(self.report.comments.get(self.current_draft_key, ""))
                elif value == "approve":
                    self.subview = "approval"
                    self.action_index = 0
            elif self.subview == "approval":
                if value == "back":
                    self.subview = "actions"
                    self.action_index = 0
                elif value == "approve":
                    self._post_draft()
            self._invalidate()

    def _apply_match(self, value: str) -> None:
        item = self._selected_item()
        if item is None:
            return
        if value == "daily":
            item.issue_key = ""
            item.include_in_daily_update = True
            message = "Activity kept in the daily update without a Jira issue."
        elif value == "exclude":
            item.issue_key = ""
            item.include_in_daily_update = False
            message = "Activity excluded from the daily update."
        else:
            item.issue_key = value
            item.include_in_daily_update = True
            message = f"Activity assigned to {value}."
        render_report(self.report, self.config)
        self.subview = "actions"
        self.action_index = 0
        self._set_notice(message)

    def _begin_edit(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        self.edit_area.text = item.summary or item.title
        self.subview = "edit"
        self.editing = True
        if self.app is not None:
            self.app.layout.focus(self.edit_area)
            self.app.invalidate()

    def _save_edit(self) -> None:
        item = self._selected_item()
        value = self.edit_area.text.strip()
        if item is not None and value:
            item.summary = value
            render_report(self.report, self.config)
            self._set_notice("Work item summary updated.")
        elif not value:
            self._set_notice("Summary cannot be empty.", error=True)
            self._invalidate()
            return
        self.editing = False
        self.subview = "actions"
        self.focus = "right"
        if self.app is not None:
            self.app.layout.focus(self.right_window)
        self._invalidate()

    def _cancel_edit(self) -> None:
        self.editing = False
        self.subview = "actions"
        self.focus = "right"
        if self.app is not None:
            self.app.layout.focus(self.right_window)
        self._set_notice("Edit cancelled.")

    def _post_draft(self) -> None:
        key = self.current_draft_key
        draft = self.report.comments.get(key, "")
        if not key or not draft or key in self.posted:
            return
        try:
            self.issues.post_comment(key, draft)
            self.posted.add(key)
            self.subview = "actions"
            self.action_index = 0
            message = (
                f"Simulated Jira comment for {key}."
                if self.demo_mode
                else f"Comment posted to {key}."
            )
            self._set_notice(message)
        except (SourceError, ValueError, OSError) as exc:
            self.subview = "actions"
            self.action_index = 0
            self._set_notice(f"Could not post to {key}: {exc}", error=True)

    def _open_view(self, view: str) -> None:
        self.view = view
        self.subview = ""
        self.focus = "left"
        self.selected_index = 0
        self.action_index = 0
        self.current_item_index = None
        self.current_draft_key = ""
        self.left_window.vertical_scroll = 0
        self.right_window.vertical_scroll = 0
        self._clear_notice()
        if self.app is not None:
            self.app.layout.focus(self.left_window)
        self._invalidate()

    def _back(self) -> None:
        if self.view == "home":
            self._set_notice("Already at the main menu. Press q to exit.")
            return
        if self.view == "items" and self.subview == "match":
            self.subview = "actions"
            self.action_index = 0
            self._set_focus("right")
            return
        if self.view == "items" and self.subview in ("actions", "edit"):
            if self.editing:
                self.editing = False
            self.subview = ""
            self.focus = "left"
            self.current_item_index = None
            if self.app is not None:
                self.app.layout.focus(self.left_window)
            self._set_notice("Returned to work items.")
            return
        if self.view == "drafts" and self.subview == "approval":
            self.subview = "actions"
            self.action_index = 0
            self._set_focus("right")
            return
        if self.view == "drafts" and self.subview == "actions":
            self.subview = ""
            self.focus = "left"
            self.current_draft_key = ""
            if self.app is not None:
                self.app.layout.focus(self.left_window)
            self._set_notice("Returned to Jira drafts.")
            return
        self._open_view("home")

    def _copy(self, value: str) -> None:
        try:
            copied = self.copy(value)
        except (OSError, RuntimeError) as exc:
            self._set_notice(f"Clipboard unavailable: {exc}", error=True)
            return
        if copied and self.demo_mode:
            self._set_notice("Demo mode: clipboard copy simulated.")
        elif copied:
            self._set_notice("Copied to clipboard.")
        else:
            self._set_notice(
                "Clipboard unavailable. Select and copy the text from the preview.",
                error=True,
            )

    def _set_notice(self, message: str, *, error: bool = False) -> None:
        self.notice = message
        self.notice_is_error = error
        self._invalidate()

    def _clear_notice(self) -> None:
        self.notice = ""
        self.notice_is_error = False

    def _invalidate(self) -> None:
        if self.app is not None:
            self.app.invalidate()

    def run(self) -> None:
        """Open Recap in the alternate screen and restore the terminal on exit."""
        if self.app is None:
            return
        self.app.run()

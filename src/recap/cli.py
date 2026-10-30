"""Terminal entry point."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .config import default_config_path, init_config, load_config
from .report import RecapService
from .sources import SourceError, discover_repos
from .timezones import resolve_timezone
from .tui import RecapTUI


def _copy(text: str) -> bool:
    if sys.platform == "darwin":
        commands = [["pbcopy"]]
    elif sys.platform == "win32":
        commands = [["clip"]]
    else:
        commands = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]
    for command in commands:
        if shutil.which(command[0]):
            result = subprocess.run(
                command, input=text, text=True, capture_output=True, check=False
            )
            return result.returncode == 0
    return False


def _selected_date(args: argparse.Namespace, tz_name: str) -> date:
    if args.date:
        return date.fromisoformat(args.date)
    zone, _label = resolve_timezone(tz_name)
    return datetime.now(UTC).astimezone(zone).date() - timedelta(
        days=args.days_ago or 0
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="recap",
        description="Prepare an end-of-day engineering activity update.",
        epilog=(
            "Configuration is loaded from ./config.toml when it exists, otherwise from the OS user config. "
            "Use --config PATH before the command to choose a file.\n\n"
            "Examples:\n"
            "  recap run\n"
            "  recap ui --days-ago 1\n"
            "  recap --config ./config.toml repos"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="Use this non-secret TOML config file instead of the default location.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands = {
        "run": (
            "Collect activity and review the update in the terminal",
            (
                "Collect activity, summarize it, and open the full-screen terminal workspace when run interactively. "
                "When output is redirected, print the update and Jira drafts instead."
            ),
        ),
        "ui": (
            "Open the local browser interface",
            "Start the loopback-only browser UI and open it in your default browser.",
        ),
    }
    for name, (help_text, description) in commands.items():
        command = sub.add_parser(
            name,
            help=help_text,
            description=description,
            epilog=(
                "Examples:\n"
                f"  recap {name}\n"
                f"  recap {name} --days-ago 1\n"
                f"  recap {name} --date 2026-01-15 --tz Etc/UTC\n\n"
                f"Use --config PATH before '{name}' to select a config file."
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        group = command.add_mutually_exclusive_group()
        group.add_argument(
            "--date",
            metavar="YYYY-MM-DD",
            help="Calendar date to report; defaults to today in the selected timezone.",
        )
        group.add_argument(
            "--days-ago",
            type=int,
            metavar="N",
            help="Report N days before today (0 means today).",
        )
        command.add_argument(
            "--tz",
            metavar="IANA-TIMEZONE",
            help="Override the configured timezone (for example, Etc/UTC).",
        )
        command.add_argument(
            "--model",
            metavar="MODEL-ID",
            help="Override the configured AI model for this run.",
        )
        command.add_argument(
            "--repo-root",
            action="append",
            type=Path,
            metavar="DIR",
            help="Override configured repository roots; repeat to search multiple directories.",
        )

    sub.add_parser(
        "models",
        help="List models offered by the configured AI endpoint",
        description="Ask the configured AI endpoint for its available model IDs.",
        epilog="Example: recap models\nUse --config PATH before 'models' to select a config file.",
    )
    repos_cmd = sub.add_parser(
        "repos",
        help="Discover Git repositories under configured roots",
        description="Recursively list Git repositories and worktrees under the selected roots.",
        epilog=(
            "Examples:\n"
            "  recap repos\n"
            "  recap repos --repo-root ~/projects --repo-root ~/work\n\n"
            "Use --config PATH before 'repos' to select a config file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    repos_cmd.add_argument(
        "--repo-root",
        action="append",
        type=Path,
        metavar="DIR",
        help="Override configured repository roots; repeat to search multiple directories.",
    )
    config_cmd = sub.add_parser(
        "config",
        help="Create or locate the non-secret config file",
        description="Initialize a config file or print the file Recap will use.",
        epilog=(
            "By default, Recap selects ./config.toml if it exists, otherwise the OS user config. "
            "Pass --config PATH before 'config' to override that selection.\n\n"
            "Examples:\n"
            "  recap config path\n"
            "  recap config init\n"
            "  recap --config ./config.toml config path"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    config_actions = config_cmd.add_subparsers(
        dest="action", metavar="ACTION", required=True
    )
    config_actions.add_parser(
        "init",
        help="Create the selected config file with default settings",
        description=(
            "Create a non-secret config file at the selected location and print its path. "
            "The command fails if that file already exists. By default, the selected location is "
            "./config.toml when present, otherwise the OS user config path."
        ),
        epilog="Use --config PATH before 'config init' to choose a specific file.",
    )
    config_actions.add_parser(
        "path",
        help="Print the selected config file path",
        description=(
            "Print the config file Recap selects for this invocation. If it does not exist, "
            "the path shows where 'config init' will create it. Recap selects ./config.toml when "
            "present, otherwise the OS user config path; --config PATH overrides the selection."
        ),
    )
    raw = argv if argv is not None else sys.argv[1:]
    if not raw:
        raw = ["run"]
    args = parser.parse_args(raw)
    path = args.config or default_config_path()
    try:
        if args.command == "config":
            if args.action == "init":
                print(init_config(path))
            else:
                print(path)
            return 0
        config = load_config(path)
        if args.command == "repos":
            if args.repo_root:
                config = replace(
                    config,
                    repo_roots=[root.expanduser().resolve() for root in args.repo_root],
                )
            warnings: list[str] = []
            found = discover_repos(config, warnings)
            print("Configured repository roots:")
            for root in config.repo_roots:
                print(f"  {root}")
            print(f"Discovered repositories ({len(found)}):")
            for repo in found:
                print(f"  {repo}")
            for warning in warnings:
                print(f"Warning: {warning}", file=sys.stderr)
            return 0
        if args.command == "models":
            for model in RecapService(config).ai.list_models():
                print(model)
            return 0
        if args.command not in ("run", "ui"):
            parser.print_help()
            return 0
        if args.days_ago is not None and args.days_ago < 0:
            raise ValueError("--days-ago must be zero or greater")
        if args.tz:
            config = replace(config, timezone=args.tz)
        if args.model:
            config = replace(config, ai_model=args.model)
        if args.repo_root:
            config = replace(
                config,
                repo_roots=[root.expanduser().resolve() for root in args.repo_root],
            )
        day = _selected_date(args, config.timezone)
        if args.command == "ui":
            from .webui import serve

            serve(config, path, day)
            return 0
        service = RecapService(config)
        print(
            f"AI destination: {service.ai.endpoint_url} (one full-report request per non-empty report)",
            file=sys.stderr,
        )
        report = service.build(day)
        for warning in report.warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        if sys.stdin.isatty() and sys.stdout.isatty():
            RecapTUI(report, config, copy=_copy, issues=service.issues).run()
        else:
            print(report.daily_text)
            for key, draft in report.comments.items():
                print(f"\n--- Draft for {key} ---\n{draft}")
        return 0
    except (ValueError, FileExistsError, OSError, SourceError) as exc:
        print(f"recap: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

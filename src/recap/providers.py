"""Activity-provider contract for additional forges such as GitLab."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from .config import Config
from .model import Activity
from .sources import collect_agent_sessions, collect_github, collect_local_git


class ActivityProvider(Protocol):
    name: str

    def collect(
        self,
        config: Config,
        day: date,
        start: datetime,
        end: datetime,
        warnings: list[str],
    ) -> list[Activity]: ...


class GitHubProvider:
    name = "github"

    def collect(
        self,
        config: Config,
        day: date,
        start: datetime,
        end: datetime,
        warnings: list[str],
    ) -> list[Activity]:
        return collect_github(day, start, end, warnings)


class LocalGitProvider:
    name = "local_git"

    def collect(
        self,
        config: Config,
        day: date,
        start: datetime,
        end: datetime,
        warnings: list[str],
    ) -> list[Activity]:
        return collect_local_git(config, start, end, warnings)


class AgentSessionProvider:
    name = "agent_sessions"

    def collect(
        self,
        config: Config,
        day: date,
        start: datetime,
        end: datetime,
        warnings: list[str],
    ) -> list[Activity]:
        return collect_agent_sessions(start, end, warnings, config.agent_sources)


DEFAULT_PROVIDERS: tuple[ActivityProvider, ...] = (
    GitHubProvider(),
    LocalGitProvider(),
    AgentSessionProvider(),
)

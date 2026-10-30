"""Non-secret configuration. Work data and credentials are never written here."""

from __future__ import annotations

import json
import os
import re
import string
import sys
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .timezones import validate_timezone_name

DEFAULT_TEMPLATE = "{date} update for {issue_key} — {issue_title}\n\n{items}\n\n{links}"
AGENT_SOURCE_OPTIONS = ("codex", "claude", "cursor")
DEFAULT_AGENT_SOURCES = list(AGENT_SOURCE_OPTIONS)
DEFAULT_TEXT = """# Recap settings. Credentials belong in environment variables only.
repo_roots = ["~/work"]
repo_excludes = ["node_modules", ".venv", "venv", "vendor", "build"]
agent_sources = ["codex", "claude", "cursor"]
timezone = "local"
jira_base_url = ""
jira_recent_days = 30
issue_provider = "jira"
ai_provider = "openai_responses"
ai_base_url = "https://api.openai.com/v1"
# Set this to a model supported by the configured endpoint.
ai_model = ""
ai_max_output_tokens = 4096
comment_template = "concise"

[templates]
concise = "{date} update for {issue_key} — {issue_title}\\n\\n{items}\\n\\n{links}"
detailed = "Work completed on {date} for {issue_key} — {issue_title}:\\n\\n{items}\\n\\nReferences:\\n{links}"

[custom]
# team = "Example team"
"""


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "recap" / "config.toml"


def default_config_path() -> Path:
    """Prefer a config in the launch directory, then use the user config path."""
    local_config = Path.cwd() / "config.toml"
    return local_config if local_config.is_file() else config_path()


@dataclass(slots=True)
class Config:
    repo_roots: list[Path] = field(default_factory=list)
    repo_excludes: list[str] = field(
        default_factory=lambda: ["node_modules", ".venv", "venv", "vendor", "build"]
    )
    agent_sources: list[str] = field(
        default_factory=lambda: list(DEFAULT_AGENT_SOURCES)
    )
    timezone: str = "local"
    jira_base_url: str = ""
    jira_recent_days: int = 30
    issue_provider: str = "jira"
    ai_provider: str = "openai_responses"
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = ""
    ai_max_output_tokens: int = 4096
    comment_template: str = "concise"
    templates: dict[str, str] = field(
        default_factory=lambda: {"concise": DEFAULT_TEMPLATE}
    )
    custom: dict[str, str] = field(default_factory=dict)


def _reject_secrets(value: Any, path: str = "") -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        name = str(key).lower()
        if name != "ai_max_output_tokens" and any(
            word in name
            for word in ("token", "password", "secret", "api_key", "credential")
        ):
            raise ValueError(f"Secrets are not allowed in the config file: {path}{key}")
        _reject_secrets(item, f"{path}{key}.")


def parse_config(text: str) -> Config:
    data = tomllib.loads(text)
    _reject_secrets(data)
    roots = data.get("repo_roots", [])
    if not isinstance(roots, list) or not all(isinstance(p, str) for p in roots):
        raise ValueError("repo_roots must be a list of directory paths")
    excludes = data.get("repo_excludes", Config().repo_excludes)
    if not isinstance(excludes, list) or not all(isinstance(p, str) for p in excludes):
        raise ValueError("repo_excludes must be a list of names")
    agent_sources = data.get("agent_sources", DEFAULT_AGENT_SOURCES)
    if not isinstance(agent_sources, list) or not all(
        isinstance(source, str) for source in agent_sources
    ):
        raise ValueError(
            "agent_sources must be a list containing codex, claude, or cursor"
        )
    unknown_sources = sorted(set(agent_sources) - set(AGENT_SOURCE_OPTIONS))
    if unknown_sources:
        raise ValueError(f"Unknown agent sources: {', '.join(unknown_sources)}")
    days = data.get("jira_recent_days", 30)
    if not isinstance(days, int) or not 1 <= days <= 365:
        raise ValueError("jira_recent_days must be between 1 and 365")
    max_output = data.get("ai_max_output_tokens", 4096)
    if (
        isinstance(max_output, bool)
        or not isinstance(max_output, int)
        or not 16 <= max_output <= 65536
    ):
        raise ValueError("ai_max_output_tokens must be between 16 and 65536")
    templates = data.get("templates", {"concise": DEFAULT_TEMPLATE})
    custom = data.get("custom", {})
    if not isinstance(templates, dict) or not all(
        isinstance(v, str) for v in templates.values()
    ):
        raise ValueError("templates must contain named strings")
    if not isinstance(custom, dict) or not all(
        isinstance(v, (str, int, float, bool)) for v in custom.values()
    ):
        raise ValueError("custom values must be simple scalar values")
    template_name = data.get("comment_template", "concise")
    if template_name not in templates:
        raise ValueError(f"comment_template {template_name!r} is not in [templates]")
    for key in (
        "timezone",
        "jira_base_url",
        "issue_provider",
        "ai_provider",
        "ai_base_url",
        "ai_model",
    ):
        if not isinstance(data.get(key, ""), str):
            raise TypeError(f"{key} must be a string")
    jira_url = data.get("jira_base_url", "").rstrip("/")
    ai_url = data.get("ai_base_url", "https://api.openai.com/v1").rstrip("/")
    jira_parts = urllib.parse.urlparse(jira_url)
    ai_parts = urllib.parse.urlparse(ai_url)
    if jira_url and (
        jira_parts.scheme != "https"
        or not jira_parts.hostname
        or jira_parts.username
        or jira_parts.password
        or jira_parts.query
        or jira_parts.fragment
    ):
        raise ValueError("jira_base_url must use HTTPS")
    if (
        not ai_parts.hostname
        or ai_parts.username
        or ai_parts.password
        or ai_parts.query
        or ai_parts.fragment
        or not (
            ai_parts.scheme == "https"
            or (
                ai_parts.scheme == "http"
                and ai_parts.hostname in ("localhost", "127.0.0.1")
            )
        )
    ):
        raise ValueError("ai_base_url must use HTTPS, or HTTP on localhost")
    tz_name = data.get("timezone", "local")
    validate_timezone_name(tz_name)
    available = {"date", "issue_key", "issue_title", "items", "links", *custom.keys()}
    for name, template in templates.items():
        for _, field_name, _, _ in string.Formatter().parse(template):
            if field_name is not None and (
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field_name)
                or field_name not in available
            ):
                raise ValueError(
                    f"Unknown or unsafe field in template {name}: {field_name}"
                )
    return Config(
        repo_roots=[Path(p).expanduser().resolve() for p in roots],
        repo_excludes=excludes,
        agent_sources=list(dict.fromkeys(agent_sources)),
        timezone=tz_name,
        jira_base_url=jira_url,
        jira_recent_days=days,
        issue_provider=data.get("issue_provider", "jira"),
        ai_provider=data.get("ai_provider", "openai_responses"),
        ai_base_url=ai_url,
        ai_model=data.get("ai_model", ""),
        ai_max_output_tokens=max_output,
        comment_template=template_name,
        templates=templates,
        custom={k: str(v) for k, v in custom.items()},
    )


def load_config(path: Path | None = None) -> Config:
    target = path or config_path()
    return (
        parse_config(target.read_text(encoding="utf-8"))
        if target.exists()
        else Config()
    )


def save_config_text(text: str, path: Path | None = None) -> None:
    parse_config(text)
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    temp.replace(target)


def config_values(config: Config) -> dict[str, Any]:
    """Return the non-secret settings used by the structured web editor."""
    return {
        "repo_roots": [str(path) for path in config.repo_roots],
        "repo_excludes": list(config.repo_excludes),
        "agent_sources": list(config.agent_sources),
        "timezone": config.timezone,
        "jira_base_url": config.jira_base_url,
        "jira_recent_days": config.jira_recent_days,
        "issue_provider": config.issue_provider,
        "ai_provider": config.ai_provider,
        "ai_base_url": config.ai_base_url,
        "ai_model": config.ai_model,
        "ai_max_output_tokens": config.ai_max_output_tokens,
        "comment_template": config.comment_template,
        "templates": dict(config.templates),
        "custom": dict(config.custom),
    }


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"Cannot write this value to TOML: {type(value).__name__}")


def config_text_with_values(
    values: dict[str, Any], current_text: str = DEFAULT_TEXT
) -> str:
    """Merge structured settings into TOML while retaining templates and custom fields."""
    data = tomllib.loads(current_text)
    editable = {
        "repo_roots",
        "repo_excludes",
        "agent_sources",
        "timezone",
        "jira_base_url",
        "jira_recent_days",
        "issue_provider",
        "ai_provider",
        "ai_base_url",
        "ai_model",
        "ai_max_output_tokens",
        "comment_template",
    }
    for key in editable:
        if key in values:
            data[key] = values[key]
    for key in ("templates", "custom"):
        if key in values:
            data[key] = values[key]

    parse_config(_serialize_config_data(data))
    return _serialize_config_data(data)


def _serialize_config_data(data: dict[str, Any]) -> str:
    lines = ["# Recap settings. Credentials belong in environment variables only."]
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_value(value)}")
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        lines.extend(("", f"[{section}]"))
        for key, value in values.items():
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def save_config_values(values: dict[str, Any], path: Path | None = None) -> None:
    target = path or config_path()
    current_text = (
        target.read_text(encoding="utf-8") if target.exists() else DEFAULT_TEXT
    )
    save_config_text(config_text_with_values(values, current_text), target)


def init_config(path: Path | None = None) -> Path:
    target = path or config_path()
    if target.exists():
        raise FileExistsError(f"Config already exists: {target}")
    save_config_text(DEFAULT_TEXT, target)
    return target

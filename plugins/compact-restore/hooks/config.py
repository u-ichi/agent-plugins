from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

DEFAULT_CONFIG_DIR = "~/.claude/compact-restore.d"
DEFAULT_DATA_DIR = "~/.claude/plugins/data/compact-restore"
DEFAULT_MAX_CHARS_EACH = 2000
DEFAULT_STRIP_PATTERNS: tuple[str, ...] = (
    r"<system-reminder\b[^>]*>[\s\S]*?</system-reminder>",
)


@dataclass(frozen=True)
class PathRewrite:
    pattern: re.Pattern[str]
    replacement: str


@dataclass(frozen=True)
class UserMessagesConfig:
    enabled: bool
    max_chars_each: int
    strip_patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class PluginConfig:
    files: tuple[str, ...]
    reread_dirs: tuple[str, ...]
    reinvoke_skills: tuple[str, ...]
    restore_tool_inputs: tuple[str, ...]
    path_rewrites: tuple[PathRewrite, ...]
    user_messages: UserMessagesConfig
    summary_instructions: tuple[str, ...]


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def truncate_utf16(text: str, max_units: int) -> str:
    if max_units <= 0 or not text:
        return ""
    if utf16_len(text) <= max_units:
        return text
    lo = 0
    hi = min(len(text), max_units)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if utf16_len(text[:mid]) <= max_units:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def split_utf16_chunk(text: str, start: int, max_units: int) -> int:
    remaining = text[start:]
    if utf16_len(remaining) <= max_units:
        return len(text)
    lo = 0
    hi = min(len(remaining), max_units)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if utf16_len(remaining[:mid]) <= max_units:
            lo = mid
        else:
            hi = mid - 1
    end = start + lo
    if end > start and end < len(text):
        window_start = max(start + max(1, (end - start) // 2), end - 500)
        newline_pos = text.rfind("\n", window_start, end)
        if newline_pos != -1 and newline_pos >= window_start:
            return newline_pos + 1
    return end


def normalize_path(raw: str, cwd: str | None = None) -> str:
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded) and cwd:
        expanded = os.path.join(os.path.expanduser(cwd), expanded)
    return os.path.normpath(expanded)


def apply_path_rewrites(path: str, rewrites: tuple[PathRewrite, ...]) -> str:
    current = path
    for rewrite in rewrites:
        current = rewrite.pattern.sub(rewrite.replacement, current)
    return os.path.normpath(current)


def is_within_dir(path: str, directory: str) -> bool:
    try:
        common = os.path.commonpath([path, directory])
    except ValueError:
        return False
    return common == directory and path != directory


def get_config_dir() -> str:
    raw = os.environ.get("COMPACT_RESTORE_CONFIG_DIR") or DEFAULT_CONFIG_DIR
    return os.path.normpath(os.path.expanduser(raw))


def get_data_dir() -> str:
    raw = os.environ.get("CLAUDE_PLUGIN_DATA") or DEFAULT_DATA_DIR
    return os.path.normpath(os.path.expanduser(raw))


def _append_unique(target: list[str], value: str) -> None:
    if value not in target:
        target.append(value)


def _compile_regex(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _extend_str_list(target: list[str], raw: Any, *, expand_path: bool = False) -> None:
    if not isinstance(raw, list):
        return
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        value = normalize_path(item) if expand_path else item
        _append_unique(target, value)


def load_config(config_dir: str | None = None) -> PluginConfig:
    directory = config_dir if config_dir is not None else get_config_dir()

    files: list[str] = []
    reread_dirs: list[str] = []
    reinvoke_skills: list[str] = []
    restore_tool_inputs: list[str] = []
    path_rewrites: list[PathRewrite] = []
    summary_instructions: list[str] = []
    user_messages_enabled = True
    user_messages_max_chars = DEFAULT_MAX_CHARS_EACH
    strip_pattern_strings: list[str] = list(DEFAULT_STRIP_PATTERNS)

    if os.path.isdir(directory):
        try:
            entries = sorted(
                name for name in os.listdir(directory) if name.endswith(".json")
            )
        except OSError:
            entries = []

        for name in entries:
            full_path = os.path.join(directory, name)
            if not os.path.isfile(full_path):
                continue
            try:
                with open(full_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue

            _extend_str_list(files, data.get("files"), expand_path=True)
            _extend_str_list(reread_dirs, data.get("reread_dirs"), expand_path=True)
            _extend_str_list(reinvoke_skills, data.get("reinvoke_skills"))
            _extend_str_list(restore_tool_inputs, data.get("restore_tool_inputs"))
            _extend_str_list(summary_instructions, data.get("summary_instructions"))

            raw_rewrites = data.get("path_rewrites")
            if isinstance(raw_rewrites, list):
                for entry in raw_rewrites:
                    if (
                        isinstance(entry, list)
                        and len(entry) == 2
                        and isinstance(entry[0], str)
                        and isinstance(entry[1], str)
                    ):
                        compiled = _compile_regex(entry[0])
                        if compiled is not None:
                            path_rewrites.append(
                                PathRewrite(pattern=compiled, replacement=entry[1])
                            )

            raw_um = data.get("user_messages")
            if isinstance(raw_um, dict):
                raw_enabled = raw_um.get("enabled")
                if isinstance(raw_enabled, bool):
                    user_messages_enabled = raw_enabled
                raw_max = raw_um.get("max_chars_each")
                if (
                    isinstance(raw_max, int)
                    and not isinstance(raw_max, bool)
                    and raw_max > 0
                ):
                    user_messages_max_chars = raw_max
                _extend_str_list(strip_pattern_strings, raw_um.get("strip"))

    compiled_strips: list[re.Pattern[str]] = []
    for pat in strip_pattern_strings:
        compiled = _compile_regex(pat)
        if compiled is not None:
            compiled_strips.append(compiled)

    return PluginConfig(
        files=tuple(files),
        reread_dirs=tuple(reread_dirs),
        reinvoke_skills=tuple(reinvoke_skills),
        restore_tool_inputs=tuple(restore_tool_inputs),
        path_rewrites=tuple(path_rewrites),
        user_messages=UserMessagesConfig(
            enabled=user_messages_enabled,
            max_chars_each=user_messages_max_chars,
            strip_patterns=tuple(compiled_strips),
        ),
        summary_instructions=tuple(summary_instructions),
    )

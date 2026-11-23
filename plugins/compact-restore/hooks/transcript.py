from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from typing import Any

from config import utf16_len

STATE_VERSION = 1
HEAD_HASH_BYTES = 256
BASE_DIR_PREFIX = "Base directory for this skill:"
BASH_READ_COMMANDS = frozenset({"cat", "head", "sed", "grep"})
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass
class SkillCall:
    raw_name: str
    base_dir: str | None = None
    body_utf16_len: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_name": self.raw_name,
            "base_dir": self.base_dir,
            "body_utf16_len": self.body_utf16_len,
        }

    @classmethod
    def from_dict(cls, data: Any) -> SkillCall | None:
        if not isinstance(data, dict):
            return None
        raw_name = data.get("raw_name")
        base_dir = data.get("base_dir")
        body_len = data.get("body_utf16_len")
        if not isinstance(raw_name, str) or not raw_name:
            return None
        if base_dir is not None and not isinstance(base_dir, str):
            return None
        if not isinstance(body_len, int) or isinstance(body_len, bool) or body_len < 0:
            return None
        return cls(raw_name=raw_name, base_dir=base_dir, body_utf16_len=body_len)


@dataclass
class EraActivity:
    read_paths: list[str] = field(default_factory=list)
    bash_md_paths: list[str] = field(default_factory=list)
    skills: list[SkillCall] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_paths": self.read_paths,
            "bash_md_paths": self.bash_md_paths,
            "skills": [s.to_dict() for s in self.skills],
        }

    @classmethod
    def from_dict(cls, data: Any) -> EraActivity | None:
        if not isinstance(data, dict):
            return None
        read_paths = data.get("read_paths")
        bash_md_paths = data.get("bash_md_paths")
        raw_skills = data.get("skills")
        if not (
            isinstance(read_paths, list)
            and all(isinstance(x, str) for x in read_paths)
            and isinstance(bash_md_paths, list)
            and all(isinstance(x, str) for x in bash_md_paths)
            and isinstance(raw_skills, list)
        ):
            return None
        skills: list[SkillCall] = []
        for item in raw_skills:
            parsed = SkillCall.from_dict(item)
            if parsed is None:
                return None
            skills.append(parsed)
        return cls(
            read_paths=list(read_paths),
            bash_md_paths=list(bash_md_paths),
            skills=skills,
        )


@dataclass
class PreToolState:
    offset: int = 0
    ino: int = 0
    dev: int = 0
    head_hash: str = ""
    boundary_count: int = 0
    before: EraActivity = field(default_factory=EraActivity)
    after: EraActivity = field(default_factory=EraActivity)
    pending_skills: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "offset": self.offset,
            "ino": self.ino,
            "dev": self.dev,
            "head_hash": self.head_hash,
            "boundary_count": self.boundary_count,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "pending_skills": self.pending_skills,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PreToolState | None:
        if not isinstance(data, dict):
            return None
        if data.get("version") != STATE_VERSION:
            return None
        offset = data.get("offset")
        ino = data.get("ino")
        dev = data.get("dev")
        head_hash = data.get("head_hash")
        boundary_count = data.get("boundary_count")
        pending_skills = data.get("pending_skills")
        if not (
            isinstance(offset, int)
            and not isinstance(offset, bool)
            and offset >= 0
            and isinstance(ino, int)
            and not isinstance(ino, bool)
            and isinstance(dev, int)
            and not isinstance(dev, bool)
            and isinstance(head_hash, str)
            and isinstance(boundary_count, int)
            and not isinstance(boundary_count, bool)
            and boundary_count >= 0
            and isinstance(pending_skills, list)
            and all(isinstance(i, int) and not isinstance(i, bool) for i in pending_skills)
        ):
            return None
        before = EraActivity.from_dict(data.get("before"))
        after = EraActivity.from_dict(data.get("after"))
        if before is None or after is None:
            return None
        if any(i < 0 or i >= len(after.skills) for i in pending_skills):
            return None
        return cls(
            offset=offset,
            ino=ino,
            dev=dev,
            head_hash=head_hash,
            boundary_count=boundary_count,
            before=before,
            after=after,
            pending_skills=list(pending_skills),
        )


@dataclass
class SessionTranscript:
    boundary_count: int
    before: EraActivity
    after: EraActivity
    before_ordered_reads: list[tuple[str, bool]]
    after_ordered_reads: list[tuple[str, bool]]
    user_messages: list[str]
    last_tool_inputs: dict[str, Any]


def resolve_transcript_path(transcript_path: str, agent_id: str | None) -> str:
    expanded = os.path.normpath(os.path.expanduser(transcript_path))
    if not agent_id:
        return expanded
    expected_name = f"agent-{agent_id}.jsonl"
    if (
        os.path.basename(expanded) == expected_name
        and os.path.basename(os.path.dirname(expanded)) == "subagents"
    ):
        return expanded
    stem, _ = os.path.splitext(expanded)
    return os.path.join(stem, "subagents", expected_name)


def conversation_key(
    session_id: str | None,
    agent_id: str | None,
    effective_transcript_path: str,
) -> str:
    digest = hashlib.sha256(effective_transcript_path.encode("utf-8")).hexdigest()[:16]
    parts: list[str] = []
    if session_id:
        clean_session = SAFE_ID_RE.sub("_", session_id)[:40].strip("_")
        if clean_session:
            parts.append(clean_session)
    if agent_id:
        clean_agent = SAFE_ID_RE.sub("_", agent_id)[:32].strip("_")
        if clean_agent:
            parts.append(f"agent-{clean_agent}")
    parts.append(digest)
    return "-".join(parts)


def atomic_write_text(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _touch_ordered(items: list[str], value: str) -> None:
    try:
        items.remove(value)
    except ValueError:
        pass
    items.append(value)


def _merge_era(target: EraActivity, source: EraActivity) -> None:
    for path in source.read_paths:
        _touch_ordered(target.read_paths, path)
    for path in source.bash_md_paths:
        _touch_ordered(target.bash_md_paths, path)
    target.skills.extend(source.skills)


def extract_bash_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        raw_tokens = list(lexer)
    except ValueError:
        raw_tokens = command.split()

    tokens: list[str] = []
    for tok in raw_tokens:
        cleaned = tok.strip("\"'`;|&()")
        if not cleaned:
            continue
        tokens.append(cleaned)
        if "=" in cleaned:
            _, rhs = cleaned.split("=", 1)
            rhs = rhs.strip("\"'`;|&()")
            if rhs:
                tokens.append(rhs)
    return tokens


def _extract_text_content(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        if parts:
            return "\n".join(parts)
    return None


def extract_user_message(record: dict[str, Any]) -> str | None:
    if record.get("type") != "user":
        return None
    if record.get("isMeta") is True or record.get("isCompactSummary") is True:
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                return None
            if btype == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        if not parts:
            return None
        text = "\n".join(parts)
    else:
        return None
    stripped_head = text.lstrip()
    if stripped_head.startswith("<local-command-") or stripped_head.startswith("<command-name>"):
        return None
    return text


def _attach_skill_meta(
    era: EraActivity,
    pending_skills: list[int],
    text: str,
) -> None:
    if not pending_skills:
        return
    stripped = text.lstrip()
    if not stripped.startswith(BASE_DIR_PREFIX):
        return
    first_line, _, rest = stripped.partition("\n")
    base_dir = first_line[len(BASE_DIR_PREFIX):].strip()
    body = rest[1:] if rest.startswith("\n") else rest
    body_len = utf16_len(body)
    folder_name = os.path.basename(base_dir.rstrip("/\\"))

    chosen_pos = 0
    for pos, skill_idx in enumerate(pending_skills):
        skill = era.skills[skill_idx]
        short_name = skill.raw_name.split(":", 1)[-1]
        if short_name == folder_name:
            chosen_pos = pos
            break

    skill_idx = pending_skills.pop(chosen_pos)
    era.skills[skill_idx].base_dir = base_dir or None
    era.skills[skill_idx].body_utf16_len = body_len


def _apply_record(
    state: PreToolState,
    record: dict[str, Any],
    *,
    collect_bash_reads: bool,
    user_messages: list[str] | None = None,
    tracked_tools: frozenset[str] = frozenset(),
    before_tool_inputs: dict[str, Any] | None = None,
    after_tool_inputs: dict[str, Any] | None = None,
    before_ordered_reads: list[tuple[str, bool]] | None = None,
    after_ordered_reads: list[tuple[str, bool]] | None = None,
) -> None:
    rec_type = record.get("type")

    if rec_type == "system" and record.get("subtype") == "compact_boundary":
        state.pending_skills.clear()
        _merge_era(state.before, state.after)
        state.after = EraActivity()
        if before_tool_inputs is not None and after_tool_inputs is not None:
            before_tool_inputs.update(after_tool_inputs)
            after_tool_inputs.clear()
        if before_ordered_reads is not None and after_ordered_reads is not None:
            before_ordered_reads.extend(after_ordered_reads)
            after_ordered_reads.clear()
        state.boundary_count += 1
        return

    if rec_type == "assistant":
        state.pending_skills.clear()
        message = record.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            tool_input = block.get("input")
            if not isinstance(name, str):
                continue
            if after_tool_inputs is not None and name in tracked_tools:
                after_tool_inputs[name] = tool_input
            if not isinstance(tool_input, dict):
                continue
            if name == "Read":
                file_path = tool_input.get("file_path")
                if isinstance(file_path, str) and file_path.strip():
                    cleaned_path = file_path.strip()
                    _touch_ordered(state.after.read_paths, cleaned_path)
                    if after_ordered_reads is not None:
                        after_ordered_reads.append((cleaned_path, False))
            elif name == "Bash":
                command = tool_input.get("command")
                if isinstance(command, str) and command.strip():
                    tokens = extract_bash_tokens(command)
                    has_read_cmd = any(
                        os.path.basename(t) in BASH_READ_COMMANDS for t in tokens
                    )
                    for tok in tokens:
                        if tok.endswith(".md"):
                            _touch_ordered(state.after.bash_md_paths, tok)
                        if collect_bash_reads and has_read_cmd and not tok.startswith("-"):
                            if "/" in tok or tok.startswith("~"):
                                if after_ordered_reads is not None:
                                    after_ordered_reads.append((tok, True))
            elif name == "Skill":
                skill_name = tool_input.get("skill")
                if isinstance(skill_name, str) and skill_name.strip():
                    state.after.skills.append(SkillCall(raw_name=skill_name.strip()))
                    state.pending_skills.append(len(state.after.skills) - 1)
        return

    if rec_type == "user":
        if record.get("isMeta") is True:
            message = record.get("message")
            if isinstance(message, dict):
                text = _extract_text_content(message.get("content"))
                if text:
                    _attach_skill_meta(state.after, state.pending_skills, text)
            return
        if user_messages is not None:
            user_text = extract_user_message(record)
            if user_text is not None:
                user_messages.append(user_text)


def _compute_head_hash(path: str) -> str:
    with open(path, "rb") as fh:
        head = fh.read(HEAD_HASH_BYTES)
    return hashlib.sha256(head).hexdigest()


def _load_cached_pretool_state(cache_path: str) -> PreToolState | None:
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    return PreToolState.from_dict(raw)


def load_pretool_state_incremental(
    transcript_path: str,
    data_dir: str,
    conv_key: str,
) -> PreToolState:
    stat = os.stat(transcript_path)
    head_hash = _compute_head_hash(transcript_path)
    cache_path = os.path.join(data_dir, f"{conv_key}.pretool.json")

    state = _load_cached_pretool_state(cache_path)
    need_save = False
    if (
        state is None
        or state.ino != stat.st_ino
        or state.dev != stat.st_dev
        or state.head_hash != head_hash
        or state.offset > stat.st_size
    ):
        state = PreToolState(
            offset=0,
            ino=stat.st_ino,
            dev=stat.st_dev,
            head_hash=head_hash,
        )
        need_save = True

    with open(transcript_path, "rb") as fh:
        fh.seek(state.offset)
        while True:
            line_start = fh.tell()
            raw_line = fh.readline()
            if not raw_line:
                break
            ends_with_newline = raw_line.endswith(b"\n")
            stripped = raw_line.strip()
            if not stripped:
                if ends_with_newline:
                    state.offset = fh.tell()
                    need_save = True
                continue
            try:
                record = json.loads(stripped.decode("utf-8", errors="replace"))
            except ValueError:
                if ends_with_newline:
                    state.offset = fh.tell()
                    need_save = True
                else:
                    state.offset = line_start
                    break
                continue
            state.offset = fh.tell()
            need_save = True
            if isinstance(record, dict):
                _apply_record(state, record, collect_bash_reads=False)

    if need_save:
        try:
            atomic_write_text(
                cache_path,
                json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":")),
            )
        except OSError:
            pass

    return state


def parse_session_transcript(
    transcript_path: str,
    tracked_tools: frozenset[str],
) -> SessionTranscript:
    state = PreToolState()
    user_messages: list[str] = []
    before_tool_inputs: dict[str, Any] = {}
    after_tool_inputs: dict[str, Any] = {}
    before_ordered_reads: list[tuple[str, bool]] = []
    after_ordered_reads: list[tuple[str, bool]] = []

    with open(transcript_path, "rb") as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(record, dict):
                _apply_record(
                    state,
                    record,
                    collect_bash_reads=True,
                    user_messages=user_messages,
                    tracked_tools=tracked_tools,
                    before_tool_inputs=before_tool_inputs,
                    after_tool_inputs=after_tool_inputs,
                    before_ordered_reads=before_ordered_reads,
                    after_ordered_reads=after_ordered_reads,
                )

    effective_tool_inputs = (
        dict(before_tool_inputs)
        if state.boundary_count > 0
        else dict(after_tool_inputs)
    )
    return SessionTranscript(
        boundary_count=state.boundary_count,
        before=state.before,
        after=state.after,
        before_ordered_reads=before_ordered_reads,
        after_ordered_reads=after_ordered_reads,
        user_messages=user_messages,
        last_tool_inputs=effective_tool_inputs,
    )

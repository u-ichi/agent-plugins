import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Skill:
    name: str
    directory: Optional[str]
    body: Optional[str]


def text_content(row):
    content = row.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"] for part in content
            if isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


def tool_calls(row):
    content = row.get("message", {}).get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                name, data = part.get("name"), part.get("input")
                if isinstance(name, str) and isinstance(data, dict):
                    yield name, data


def user_text(row):
    if row.get("isMeta") or row.get("isCompactSummary"):
        return None
    content = row.get("message", {}).get("content")
    if isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "tool_result"
        for part in content
    ):
        return None
    text = text_content(row)
    if not text or text.lstrip().startswith(("<local-command-", "<command-name>")):
        return None
    return text


def skill_invocations(names, meta=""):
    prefix = "Base directory for this skill: "
    directory = None
    body = meta or None
    if meta.startswith(prefix):
        first, _, body = meta.partition("\n")
        directory = first[len(prefix):].strip()
        if body.startswith("\n"):
            body = body[1:]

    for name in names:
        leaf = name.rsplit(":", 1)[-1]
        path = Path(directory) if directory else None
        if path and (len(names) == 1 or path.name == leaf):
            if ":" not in name and path.name == leaf and path.parent.name == "skills":
                plugin = re.sub(r"~g\d+$", "", path.parent.parent.name)
                if plugin:
                    name = f"{plugin}:{name}"
            yield Skill(name, directory, body)
        else:
            yield Skill(name, None, body if len(names) == 1 and not path else None)


def events(stream, pending_skills):
    while True:
        offset = stream.tell()
        raw = stream.readline()
        if not raw:
            break
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError):
            if not raw.endswith(b"\n"):
                stream.seek(offset)
                break
            continue
        if not isinstance(row, dict):
            continue
        message = row.get("message", {})
        if not isinstance(message, dict):
            continue
        kind = row.get("type")
        if pending_skills:
            meta = text_content(row) if kind == "user" and row.get("isMeta") else ""
            yield from (("skill", skill) for skill in skill_invocations(pending_skills, meta))
            pending_skills.clear()

        if kind == "system" and row.get("subtype") == "compact_boundary":
            yield "boundary", None
        elif kind == "assistant":
            for name, data in tool_calls(row):
                skill_name = data.get("skill")
                if name == "Skill" and isinstance(skill_name, str) and skill_name:
                    pending_skills.append(skill_name)
                yield "tool", (name, data)
        elif kind == "user":
            text = user_text(row)
            if text is not None:
                yield "user", text

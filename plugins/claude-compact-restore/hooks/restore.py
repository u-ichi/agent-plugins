import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
from typing import NamedTuple


LIMIT = 9000
INTRO = (
    "会話が compact されたため、プロジェクト指示の全文を入れ直します。"
    "プロジェクト指示は要約の言い換えではなく、以下の全文に従ってください。"
    "会話で決まったことと依頼されたことは、要約のとおり続けてください。\n\n"
)
CONTINUATION = "プロジェクト指示の続きです。\n\n"


CHUNK_CHARS = LIMIT
CHUNK_LINES = 180
PROJECT_INSTRUCTIONS = re.compile(
    r'<project-instructions nonce="([0-9a-fA-F]+)"(\s[^>]*)?>'
    r'((?:(?!<project-instructions nonce="[0-9a-fA-F]+"(?:\s[^>]*)?>).)*?)'
    r'</project-instructions nonce="\1">',
    re.S,
)
INSTRUCTION_ATTRIBUTE = re.compile(r'([^\s=]+)="([^"]*)"')
SKILL_BASE = re.compile(r"^Base directory for this skill: (.+)$", re.M)
READABLE_SKILL_KEY = re.compile(r"""(?:name|description|'name'|'description'|"name"|"description")[ \t]*:(?:\s|$)""")
SKILL_SUBSTITUTIONS = ("$ARGUMENTS", "${CLAUDE_", "!`")
REPLY_TOOLS = {"mcp__hearthbot__" + name for name in (
    "post_message", "edit_message", "message_thread", "message_coordinator", "request_decision", "no_reply_needed",
    "start_thread", "post_widget",
)} | {"Agent"}
SHOWN_WHILE_CALLING = {"mcp__hearthbot__post_message", "mcp__hearthbot__post_widget"}
COMPACT_EXEMPT_TOOLS = {"Skill", "ToolSearch"} | SHOWN_WHILE_CALLING
DENY_REASON = (
    "読んだ plugin の現在の Markdown を、この呼び出しに差し込みました。"
    "差し込んだ内容に従って見直してから、もう一度呼んでください。"
)


class Transcript(NamedTuple):
    before: dict
    after: dict
    attached: set
    files: set


class SyncedFile(NamedTuple):
    account: Path
    plugin: str
    relative: Path


def fields(value):
    return value if isinstance(value, dict) else {}


def state_path(event):
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id or Path(session_id).name != session_id:
        return None
    root = os.environ.get("CLAUDE_COMPACT_RESTORE_STATE_DIR")
    directory = Path(root) if root is not None else Path(tempfile.gettempdir()) / "claude-compact-restore"
    return directory / f"{session_id}.json"


def edit_state(event, change):
    path = state_path(event)
    if path is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            snapshot = file.read()
            state = json.loads(snapshot) if snapshot else {"files": {}, "before": {}, "after": {}, "skill_bases": []}
            before = json.dumps(state, sort_keys=True)
            result = change(state)
            if not snapshot or json.dumps(state, sort_keys=True) != before:
                file.seek(0)
                json.dump(state, file)
                file.truncate()
            return result
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return None


def transcript_entries(event, markers):
    path = event.get("transcript_path")
    if not isinstance(path, str) or not path:
        raise FileNotFoundError
    with open(path, encoding="utf-8") as transcript:
        for line in transcript:
            if markers and not any(marker in line for marker in markers):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def text_blocks(content):
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if fields(block).get("type") == "text" and isinstance(block.get("text"), str):
                yield block["text"]


def extract_instructions(text):
    match = PROJECT_INSTRUCTIONS.search(text)
    return match[3].strip() if match else None


def save_instructions(event):
    prompt = event.get("prompt")
    text = extract_instructions(prompt) if isinstance(prompt, str) else None
    if text is not None:
        edit_state(event, lambda state: state.update(instructions=text))


def latest_instructions(event):
    latest = None
    parts = {}
    try:
        for row in transcript_entries(event, ()):
            if row.get("type") == "user":
                texts = ("\n".join(text_blocks(fields(row.get("message")).get("content"))),)
            elif row.get("type") == "attachment":
                attachment = fields(row.get("attachment"))
                content = attachment.get("content")
                if attachment.get("type") != "hook_additional_context" or not isinstance(content, list):
                    continue
                texts = (text for text in content if isinstance(text, str))
            else:
                continue
            for text in texts:
                for nonce, attributes, body in PROJECT_INSTRUCTIONS.findall(text):
                    part = dict(INSTRUCTION_ATTRIBUTE.findall(attributes)).get("part")
                    if part is None:
                        latest = body.strip()
                        continue
                    if not re.fullmatch(r"[0-9]+/[0-9]+", part):
                        continue
                    try:
                        index, total = map(int, part.split("/"))
                    except ValueError:
                        continue
                    if not 1 <= index <= total:
                        continue
                    pieces = parts.setdefault((nonce, total), {})
                    pieces[index] = body
                    if len(pieces) == total:
                        latest = "".join(pieces[k] for k in range(1, total + 1)).strip()
    except (OSError, UnicodeError):
        return None
    return latest


def forget_files(state):
    state["files"] = dict.fromkeys(state["files"])
    state.pop("restored_skills", None)
    state.pop("sending", None)
    state["before"].update(state["after"])
    state["after"].clear()
    return state.get("instructions")


def units(text):
    return len(text.encode("utf-16-le")) // 2


def instruction_parts(text):
    size = units(text)
    first_budget = LIMIT - units(INTRO)
    if size <= first_budget:
        return [INTRO + text]

    second_budget = LIMIT - units(CONTINUATION)
    if size > first_budget + second_budget:
        return []

    length = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        length += units(line)
        if length > first_budget:
            break
        offset += len(line)
        if size - length <= second_budget:
            return [INTRO + text[:offset], CONTINUATION + text[offset:]]
    return []


def restore_instructions(event, part):
    if event.get("source") != "compact":
        return None

    text = latest_instructions(event)
    saved = None
    if part == 1:
        saved = edit_state(event, forget_files)
    elif text is None:
        saved = edit_state(event, lambda state: state.get("instructions"))
    if text is None:
        text = saved
    if not isinstance(text, str) or not text:
        return None

    parts = instruction_parts(text)
    if not parts and part == 1:
        path = state_path(event)
        if path is None:
            return None
        destination = path.with_name(f"{path.stem}-instructions.md")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(text)
        parts = [f"プロジェクト指示を書いたファイル {destination} を Read で読み直してから続けてください。"]
    if part <= len(parts):
        return {"hookEventName": "SessionStart", "additionalContext": parts[part - 1]}
    return None


def tool_uses(row):
    content = fields(row.get("message")).get("content")
    for block in content if isinstance(content, list) else []:
        if fields(block).get("type") == "tool_use":
            yield block


def scan_transcript(event):
    before, after, attached, files = {}, {}, set(), set()
    failed = set()
    compacted = False
    try:
        markers = ('"compact_boundary"', '"Skill"', '"attachment"', '"is_error"')
        for row in transcript_entries(event, markers):
            row_type = row.get("type")
            if row_type == "system" and row.get("subtype") == "compact_boundary":
                before.update(after)
                after.clear()
                attached.clear()
                files.clear()
                compacted = True
            elif row_type == "assistant":
                for call in tool_uses(row):
                    name = fields(call.get("input")).get("skill")
                    if call.get("name") == "Skill" and isinstance(name, str) and name:
                        call_id = call.get("id")
                        after[name, call_id if isinstance(call_id, str) else None] = None
            elif row_type == "user":
                content = fields(row.get("message")).get("content")
                for block in content if isinstance(content, list) else []:
                    if (fields(block).get("type") == "tool_result" and block.get("is_error")
                            and isinstance(block.get("tool_use_id"), str)):
                        failed.add(block["tool_use_id"])
            elif row_type == "attachment" and compacted:
                attachment = fields(row.get("attachment"))
                kind = attachment.get("type")
                cwd = row.get("cwd") or event.get("cwd")
                if kind == "invoked_skills" and isinstance(attachment.get("skills"), list):
                    for skill in attachment["skills"]:
                        name = fields(skill).get("name")
                        if isinstance(name, str):
                            attached.add(name)
                        content = fields(skill).get("content")
                        if isinstance(content, str):
                            first_line = content.partition("\n")[0]
                            for base in skill_bases(first_line, cwd):
                                path = real_path(base / "SKILL.md")
                                if path is not None:
                                    files.add(markdown_identity(path))
                elif kind == "file" and isinstance(attachment.get("filename"), str):
                    path = real_path(attachment["filename"], cwd)
                    if path is not None:
                        files.add(markdown_identity(path))
    except (OSError, UnicodeDecodeError):
        return None
    before = dict.fromkeys(name for name, call_id in before if call_id not in failed)
    after = dict.fromkeys(name for name, call_id in after if call_id not in failed)
    return Transcript(before, after, attached, files)


def short_name(name):
    return name.rsplit(":", 1)[-1]


def missing_skills(before, restored):
    restored_short_names = {short_name(name) for name in restored}
    missing = []
    for name in before:
        short = short_name(name)
        if name in restored or short in restored:
            continue
        if ":" not in name and name in restored_short_names:
            continue
        missing.append(name)
    qualified_short_names = {short_name(name) for name in missing if ":" in name}
    return [name for name in missing if name not in qualified_short_names]


def check_skills_reinvoked(event, transcript, state, manifests):
    if transcript is not None:
        missing = missing_skills(transcript.before, transcript.attached | transcript.after.keys())
    elif state is not None:
        missing = missing_skills(state["before"], state["after"])
    else:
        return [], None

    paths, recalls = [], []
    for name in missing:
        path = readable_skill(name, manifests)
        if path is not None:
            paths.append(path)
        else:
            recalls.append(name)
    if recalls and event["tool_name"] not in COMPACT_EXEMPT_TOOLS:
        names = "、".join(recalls)
        return paths, (
            "会話が compact された後、前に呼んだ skill がありません。"
            f"Skill ツールで「{names}」を呼び直してから続けてください。"
        )
    return paths, None


def real_path(path, cwd=None):
    try:
        return (Path(cwd or Path.cwd()) / Path(path).expanduser()).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


@lru_cache(maxsize=1)
def synced_root():
    return real_path(Path.home() / ".claude/plugins/synced")


def markdown_identity(path):
    root = synced_root()
    if root is not None and path.is_relative_to(root):
        parts = path.relative_to(root).parts
        if len(parts) >= 3:
            account, folder, *relative = parts
            plugin = re.sub(r"~g(?:[2-9]|[1-9][0-9]+)$", "", folder)
            return SyncedFile(root / account, plugin, Path(*relative))
    return path


def manifest_generations(account):
    try:
        manifest = fields(json.loads((account / "manifest.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    plugins = manifest.get("plugins")
    if not isinstance(plugins, list):
        return {}
    return {
        entry["name"]: entry.get("generation", 1)
        for entry in plugins if isinstance(fields(entry).get("name"), str)
    }


def current_markdown(identity, recorded, manifests):
    if not isinstance(identity, SyncedFile):
        return recorded
    account, plugin, relative = identity
    if account not in manifests:
        manifests[account] = manifest_generations(account)
    generation = manifests[account].get(plugin)
    if type(generation) is not int or generation < 1:
        return recorded
    folder = account / (plugin if generation == 1 else f"{plugin}~g{generation}")
    try:
        return folder / relative if folder.is_dir() else recorded
    except OSError:
        return recorded


def readable_skill(name, manifests):
    if not isinstance(name, str):
        return None
    plugin, separator, skill = name.partition(":")
    if not separator:
        skill = plugin
    if not skill or Path(skill).name != skill or skill == "..":
        return None
    root = synced_root()
    if root is None:
        return None

    relative = Path("skills") / skill / "SKILL.md"
    matches = []
    try:
        for account in root.iterdir():
            if account not in manifests:
                manifests[account] = manifest_generations(account)
            plugins = (plugin,) if separator else manifests[account]
            for candidate in plugins:
                if Path(candidate).name != candidate or candidate in ("", ".."):
                    continue
                path = current_markdown(SyncedFile(account, candidate, relative), None, manifests)
                if path is not None and path.is_file() and real_path(path) == path:
                    matches.append(path)
                    if len(matches) > 1:
                        return None
        if not matches:
            return None
        [path] = matches
        first, _, rest = path.read_text(encoding="utf-8").partition("\n")
    except (OSError, UnicodeError):
        return None
    if first != "---":
        return None

    lines = iter(rest.split("\n"))
    frontmatter = []
    for line in lines:
        if line == "---":
            break
        if line.strip() and not line.lstrip().startswith("#"):
            frontmatter.append(line)
    else:
        return None
    if not frontmatter:
        return None
    indent = min(len(line) - len(line.lstrip(" ")) for line in frontmatter)
    keys = (line[indent:] for line in frontmatter if line[indent] != " ")
    if not all(READABLE_SKILL_KEY.match(key) for key in keys):
        return None
    body = "\n".join(lines)
    return None if any(marker in body for marker in SKILL_SUBSTITUTIONS) else path


def skill_bases(content, cwd):
    for text in text_blocks(content):
        for base in SKILL_BASE.findall(text):
            path = real_path(base.strip(), cwd)
            if path is not None:
                yield path


def transcript_skill_bases(event):
    calls = set()
    try:
        for row in transcript_entries(event, ('"Skill"', 'Base directory for this skill')):
            content = fields(row.get("message")).get("content")
            cwd = row.get("cwd") or event.get("cwd")
            if row.get("type") == "assistant":
                calls.update(call["id"] for call in tool_uses(row) if call.get("name") == "Skill" and call.get("id"))
            elif row.get("type") == "user":
                if row.get("isMeta") is True:
                    yield from skill_bases(content, cwd)
                for block in content if isinstance(content, list) else []:
                    if (fields(block).get("type") == "tool_result" and not block.get("is_error")
                            and block.get("tool_use_id") in calls):
                        yield from skill_bases(block.get("content"), cwd)
    except (OSError, UnicodeError):
        return


def record_skill(event, args, response):
    requested = args.get("skill")
    if not isinstance(requested, str) or not requested:
        return
    name = fields(response).get("commandName")
    if not isinstance(name, str) or not name or (":" not in name and ":" in requested):
        name = requested
    content = response.get("content", response.get("text")) if isinstance(response, dict) else response

    def change(state):
        state["after"][name] = None
        bases = set(state["skill_bases"])
        for base in skill_bases(content, event.get("cwd")):
            bases.add(str(base))
            path = real_path(base / "SKILL.md")
            if path is not None:
                mark_delivered(state, path, recorded_only=True)
        state["skill_bases"] = sorted(bases)

    edit_state(event, change)


def markdown_path(path, roots):
    return (
        path is not None and path.suffix == ".md"
        and any(path != root and path.is_relative_to(root) for root in roots)
    )


def whole_read(args, result, cwd):
    file = result.get("file") or {}
    if not isinstance(file, dict) or file.get("truncatedByTokenCap"):
        return None
    if "offset" in args or "limit" in args:
        return None
    start, count, total = file.get("startLine"), file.get("numLines"), file.get("totalLines")
    if isinstance(total, int) and (start != 1 or not isinstance(count, int) or count < total):
        return None
    path = (args.get("file_path") or file.get("filePath") or file.get("file_path")
            or result.get("file_path") or result.get("filePath"))
    return real_path(path, cwd) if isinstance(path, str) else None


def record_read(args, response, cwd, roots):
    path = whole_read(args, response, cwd)
    if markdown_path(path, roots) and path.is_file():
        return [path]
    return []


def bash_output(response, command, cwd):
    if any(response.get(flag) for flag in (
            "persistedOutputPath", "persistedOutputSize", "stdoutTruncated", "outputTruncated",
            "isTruncated", "truncated", "truncatedByTokenCap", "interrupted")):
        return None, None
    stdout = response.get("stdout")
    if not isinstance(stdout, str):
        return None, None
    if "\n" in command.strip():
        return stdout, None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return stdout, None
    if not words or words[0] not in ("cat", "/bin/cat", "/usr/bin/cat"):
        return stdout, None
    args = words[2:] if words[1:2] == ["--"] else words[1:]
    if len(args) == 1 and not args[0].startswith("-") and not any(c in args[0] for c in "$`*?["):
        return stdout, real_path(args[0], cwd)
    return stdout, None


def bash_paths(command, cwd, roots):
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        words = []
    for word in words:
        if word.endswith(".md"):
            path = real_path(word, cwd)
            if markdown_path(path, roots):
                yield path


def record_bash(args, response, cwd, roots):
    command = args.get("command", "")
    stdout, cat = bash_output(response, command, cwd)
    output = (stdout or "").rstrip("\n")
    if len(output) < 200:
        return []
    if cat is not None:
        return [cat] if markdown_path(cat, roots) else []

    named = set(bash_paths(command, cwd, roots))
    candidates = named.copy()
    for root in roots:
        for directory, _, names in os.walk(root):
            for name in names:
                if name.endswith(".md"):
                    path = real_path(Path(directory) / name)
                    if markdown_path(path, roots):
                        candidates.add(path)
    surrounded = "\n" + output + "\n"
    groups = {}
    for path in sorted(candidates):
        try:
            content = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            continue
        text = content.rstrip("\n")
        if len(content) >= 200 and text and "\n" + text + "\n" in surrounded:
            groups.setdefault(text, []).append(path)
    return [
        path for group in groups.values() for path in group
        if len(group) == 1 or path in named or re.search(
            r"(?<![\w./-])" + re.escape(str(path)) + r"(?![\w./-])", command
        )
    ]


def mark_delivered(state, path, *, recorded_only=False):
    files = state["files"]
    key = str(path)
    identity = markdown_identity(path)
    matching = [key] if key in files else []
    if isinstance(identity, SyncedFile):
        matching = [old for old in files if markdown_identity(Path(old)) == identity]
    if recorded_only and not matching:
        return
    for old in matching:
        if old != key:
            del files[old]
    files[key] = True
    if (state.get("sending") or {}).get("key") in matching:
        state.pop("sending", None)


def record_tool(event):
    tool = event.get("tool_name")
    args, response = fields(event.get("tool_input")), event.get("tool_response")
    result = fields(response)
    if result.get("is_error") or result.get("isError") or result.get("success") is False:
        return
    if tool == "Skill":
        record_skill(event, args, response)
        return

    recorder = {"Read": record_read, "Bash": record_bash}.get(tool)
    if recorder is None:
        return

    def change(state):
        roots = {Path.home().joinpath(".claude", "plugins").resolve()}
        roots.update(Path(base) for base in state["skill_bases"])
        roots.update(transcript_skill_bases(event))
        for path in recorder(args, result, event.get("cwd"), roots):
            mark_delivered(state, path)

    edit_state(event, change)


def chunks(text, budget):
    parts, part, length = [], [], 0
    for line in text.splitlines(keepends=True):
        size = units(line)
        while size > budget:
            lo, hi = 1, min(len(line), budget)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if units(line[:mid]) <= budget:
                    lo = mid
                else:
                    hi = mid - 1
            if part:
                parts.append("".join(part))
                part, length = [], 0
            parts.append(line[:lo])
            line = line[lo:]
            size = units(line)
        if part and (length + size > budget or len(part) >= CHUNK_LINES):
            parts.append("".join(part))
            part, length = [], 0
        part.append(line)
        length += size
    if part:
        parts.append("".join(part))
    return parts or [""]


def header(path, index, total):
    return (
        f"hook が plugin の現在の Markdown を差し込みます（{index}/{total}、{path}）。"
        "前に読んだものの代わりに、これに従ってください。\n\n"
    )


def inject(state, attached, paths, manifests):
    files = state["files"]
    sending = state.get("sending") or {}
    recorded = {markdown_identity(Path(key)): (key, files) for key in files}
    restored = state.setdefault("restored_skills", {}) if paths else {}
    restored_identities = {markdown_identity(Path(key)) for key in restored}
    for path in paths:
        identity = markdown_identity(path)
        if identity not in restored_identities:
            recorded.setdefault(identity, (str(path), restored))
    texts = []
    for identity, (key, delivered) in recorded.items():
        if delivered.get(key):
            continue
        if identity in attached:
            delivered[key] = True
            continue
        path = current_markdown(identity, Path(key), manifests)
        try:
            content = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            continue
        # 見出しも上限に含める。分割数は本文の UTF-16 単位数を超えない。
        size = max(1, units(content))
        budget = CHUNK_CHARS - units(header(path, size, size))
        if budget < 2:
            continue
        parts = chunks(content, budget)
        version = hashlib.sha256(content.encode("utf-8")).hexdigest()
        same = (sending.get("key") == key and sending.get("path", key) == str(path)
                and sending.get("version") == version)
        index = sending["next"] if same and sending["next"] < len(parts) else 0
        text = header(path, index + 1, len(parts)) + parts[index]
        joined = "\n\n".join(texts + [text])
        if texts and (units(joined) > CHUNK_CHARS or joined.count("\n") >= CHUNK_LINES + 10):
            break
        texts.append(text)
        if index + 1 < len(parts):
            state["sending"] = {"key": key, "path": str(path), "version": version, "next": index + 1}
            return joined
        delivered[key] = True
    state.pop("sending", None)
    return "\n\n".join(texts)


def pre_tool_use(event):
    manifests = {}
    if event["tool_name"] == "Skill":
        name = fields(event.get("tool_input")).get("skill")
        path = readable_skill(name, manifests)
        if path is not None:
            return {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"skill「{name}」は Read ツールで {path} を全文読んでください。\n"
                    f"Base directory for this skill: {path.parent}\n"
                    "compact の後に Claude Code が古い本文を付け直さないようにするためです。"
                ),
            }
    transcript = scan_transcript(event)

    def change(state):
        paths, reason = check_skills_reinvoked(event, transcript, state, manifests)
        return inject(state, transcript.files if transcript else set(), paths, manifests), reason

    result = edit_state(event, change)
    if result is None:
        _, reason = check_skills_reinvoked(event, transcript, None, manifests)
        context = None
    else:
        context, reason = result
    if context and event["tool_name"] in REPLY_TOOLS - SHOWN_WHILE_CALLING:
        reason = f"{reason}\n{DENY_REASON}" if reason else DENY_REASON
    output = {"hookEventName": "PreToolUse"}
    if context:
        output["additionalContext"] = context
    if reason:
        output.update(permissionDecision="deny", permissionDecisionReason=reason)
    return output if context or reason else None


def main():
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict) or "agent_id" in event:
            return
        hook = event.get("hook_event_name")
        output = None
        if hook == "SessionStart" and sys.argv[1:] in (["1"], ["2"]):
            output = restore_instructions(event, int(sys.argv[1]))
        elif hook == "UserPromptSubmit":
            save_instructions(event)
        elif hook == "PostToolUse":
            record_tool(event)
        elif hook == "PreToolUse":
            output = pre_tool_use(event)
        if output:
            print(json.dumps({"hookSpecificOutput": output}))
    except (OSError, ValueError, RuntimeError, TypeError, KeyError):
        return


if __name__ == "__main__":
    main()

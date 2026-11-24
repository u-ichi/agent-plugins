import fnmatch
import json
import os
from pathlib import Path
import sys


LIMIT = 9000
INTRO = (
    "会話が compact されたため、プロジェクト指示の全文を入れ直します。"
    "プロジェクト指示は要約の言い換えではなく、以下の全文に従ってください。"
    "会話で決まったことと依頼されたことは、要約のとおり続けてください。\n\n"
)
CONTINUATION = "プロジェクト指示の続きです。\n\n"


def config_value(key):
    variable = "CLAUDE_COMPACT_RESTORE_" + key.upper()
    if variable in os.environ:
        value = os.environ[variable]
        return value.split(",") if key == "skills" else value

    path = Path(os.environ.get("CLAUDE_COMPACT_RESTORE_CONFIG", "~/.claude/claude-compact-restore.json"))
    config = json.loads(path.expanduser().read_text(encoding="utf-8"))
    return config.get(key) if isinstance(config, dict) else None


def utf16_units(text):
    return len(text.encode("utf-16-le")) // 2


def instruction_parts(text):
    size = utf16_units(text)
    first_budget = LIMIT - utf16_units(INTRO)
    if size <= first_budget:
        return [INTRO + text]

    second_budget = LIMIT - utf16_units(CONTINUATION)
    if size > first_budget + second_budget:
        return []

    units = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        units += utf16_units(line)
        if units > first_budget:
            break
        offset += len(line)
        if size - units <= second_budget:
            return [INTRO + text[:offset], CONTINUATION + text[offset:]]
    return []


def restore_instructions(event, part):
    if event.get("source") != "compact":
        return

    configured_path = config_value("instructions")
    if not isinstance(configured_path, str) or not configured_path:
        return

    path = Path(configured_path).expanduser()
    with path.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    if not text:
        return

    parts = instruction_parts(text)
    if not parts:
        parts = [f"プロジェクト指示を書いたファイル {path} を Read で読み直してから続けてください。"]
    if part <= len(parts):
        return {
            "hookEventName": "SessionStart",
            "additionalContext": parts[part - 1],
        }


def skill_calls(row):
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") == "Skill" and isinstance(block.get("input"), dict):
            name = block["input"].get("skill")
            if isinstance(name, str) and name:
                yield name


def short_name(name):
    return name.rsplit(":", 1)[-1]


def missing_skills(path, patterns):
    prior_calls = {}
    current_calls = {}
    attached = set()
    with Path(path).open(encoding="utf-8") as transcript:
        for line in transcript:
            # 大きなツール結果などを JSON に変換せずに読み飛ばす。
            if not (
                '"compact_boundary"' in line
                or '"invoked_skills"' in line
                or ('"assistant"' in line and '"tool_use"' in line and '"Skill"' in line)
            ):
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            row_type = row.get("type")
            if row_type == "system" and row.get("subtype") == "compact_boundary":
                prior_calls.update(current_calls)
                current_calls.clear()
                attached.clear()
            elif row_type == "assistant":
                current_calls.update(dict.fromkeys(skill_calls(row)))
            elif row_type == "attachment":
                attachment = row.get("attachment")
                if not isinstance(attachment, dict) or attachment.get("type") != "invoked_skills":
                    continue
                skills = attachment.get("skills")
                if isinstance(skills, list):
                    attached.update(
                        skill["name"]
                        for skill in skills
                        if isinstance(skill, dict) and isinstance(skill.get("name"), str)
                    )

    restored = attached | current_calls.keys()
    restored_short_names = {short_name(name) for name in restored}
    missing = []
    for name in prior_calls:
        short = short_name(name)
        if name in restored or short in restored:
            continue
        if ":" not in name and name in restored_short_names:
            continue
        if any(
            fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(short, pattern)
            for pattern in patterns
        ):
            missing.append(name)

    qualified_short_names = {short_name(name) for name in missing if ":" in name}
    return [name for name in missing if name not in qualified_short_names]


def restore_skills(event):
    if event.get("tool_name") in ("Skill", "ToolSearch"):
        return
    path = event.get("transcript_path")
    if not isinstance(path, str) or not path:
        return
    patterns = config_value("skills")
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        return
    patterns = [pattern for pattern in patterns if pattern]
    if not patterns:
        return

    missing = missing_skills(path, patterns)
    if missing:
        names = "、".join(missing)
        return {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "compact の後に Claude Code が付け直さなかった skill があります。"
                f"Skill ツールで「{names}」を呼び直してから続けてください。"
            ),
        }


def main():
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict) or "agent_id" in event:
            return
        args = sys.argv[1:]
        if args in (["1"], ["2"]):
            output = restore_instructions(event, int(args[0]))
        elif not args:
            output = restore_skills(event)
        else:
            return
        if output:
            print(json.dumps({"hookSpecificOutput": output}))
    except (OSError, ValueError, RuntimeError):
        return


if __name__ == "__main__":
    main()

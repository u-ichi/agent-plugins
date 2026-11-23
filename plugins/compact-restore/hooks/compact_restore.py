from __future__ import annotations

import json
import os
import sys
from typing import Any

sys.dont_write_bytecode = True

from config import get_data_dir, load_config
from restore import (
    ALLOWED_PRETOOL_NAMES,
    FILE_SLOT_COUNT,
    build_context_slot,
    build_precompact_output,
    build_user_messages_slot,
    compute_pending_restores,
    format_pretool_deny_reason,
    plan_file_slots,
)
from transcript import (
    conversation_key,
    load_pretool_state_incremental,
    parse_session_transcript,
    resolve_transcript_path,
)


def _read_hook_input() -> dict[str, Any] | None:
    raw = sys.stdin.read()
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _emit_session_start(context: str) -> None:
    if not context:
        return
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def _emit_pretool_deny(reason: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def _handle_pre_compact(hook_input: dict[str, Any]) -> int:
    raw_transcript = _optional_str(hook_input, "transcript_path")
    agent_id = _optional_str(hook_input, "agent_id")
    if raw_transcript is not None:
        effective = resolve_transcript_path(raw_transcript, agent_id)
        if not os.path.isfile(effective):
            return 0
    config = load_config()
    output = build_precompact_output(config)
    if output:
        sys.stdout.write(output)
    return 0


def _handle_session_start(hook_input: dict[str, Any], args: list[str]) -> int:
    source = _optional_str(hook_input, "source")
    if source is not None and source != "compact":
        return 0

    raw_transcript = _optional_str(hook_input, "transcript_path")
    if raw_transcript is None:
        return 0
    agent_id = _optional_str(hook_input, "agent_id")
    effective_transcript = resolve_transcript_path(raw_transcript, agent_id)
    if not os.path.isfile(effective_transcript):
        return 0

    if not args:
        return 0

    config = load_config()
    mode = args[0]
    is_subagent = agent_id is not None

    if mode == "files":
        if is_subagent or len(args) < 2:
            return 0
        try:
            slot_index = int(args[1])
        except ValueError:
            return 0
        if slot_index < 0 or slot_index >= FILE_SLOT_COUNT:
            return 0
        plan = plan_file_slots(config.files)
        if slot_index < len(plan.slots):
            _emit_session_start(plan.slots[slot_index])
        return 0

    if mode == "user-messages":
        session = parse_session_transcript(effective_transcript, frozenset())
        session_id = _optional_str(hook_input, "session_id")
        data_dir = get_data_dir()
        conv_key = conversation_key(session_id, agent_id, effective_transcript)
        output = build_user_messages_slot(
            session.user_messages,
            config,
            data_dir,
            conv_key,
        )
        _emit_session_start(output)
        return 0

    if mode == "context":
        tracked = (
            frozenset()
            if is_subagent
            else frozenset(config.restore_tool_inputs)
        )
        session = parse_session_transcript(effective_transcript, tracked)
        cwd = _optional_str(hook_input, "cwd")
        output = build_context_slot(
            session,
            config,
            is_subagent=is_subagent,
            cwd=cwd,
        )
        _emit_session_start(output)
        return 0

    return 0


def _handle_pre_tool_use(hook_input: dict[str, Any]) -> int:
    tool_name = _optional_str(hook_input, "tool_name")
    if tool_name is None or tool_name in ALLOWED_PRETOOL_NAMES:
        return 0

    raw_transcript = _optional_str(hook_input, "transcript_path")
    if raw_transcript is None:
        return 0
    agent_id = _optional_str(hook_input, "agent_id")
    effective_transcript = resolve_transcript_path(raw_transcript, agent_id)
    if not os.path.isfile(effective_transcript):
        return 0

    config = load_config()
    is_subagent = agent_id is not None
    if not config.reread_dirs and not config.reinvoke_skills and (
        is_subagent or not config.files
    ):
        return 0

    session_id = _optional_str(hook_input, "session_id")
    data_dir = get_data_dir()
    conv_key = conversation_key(session_id, agent_id, effective_transcript)
    state = load_pretool_state_incremental(effective_transcript, data_dir, conv_key)

    cwd = _optional_str(hook_input, "cwd")
    pending = compute_pending_restores(
        state,
        config,
        is_subagent=is_subagent,
        cwd=cwd,
    )
    if pending.read_paths or pending.skill_names:
        _emit_pretool_deny(format_pretool_deny_reason(pending))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    hook_input = _read_hook_input()
    if hook_input is None:
        return 0

    command = argv[1]
    if command == "pre-compact":
        return _handle_pre_compact(hook_input)
    if command == "session-start":
        return _handle_session_start(hook_input, argv[2:])
    if command == "pre-tool-use":
        return _handle_pre_tool_use(hook_input)
    return 0


if __name__ == "__main__":
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
HOOKS = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]
BOUNDARY = {"type": "system", "subtype": "compact_boundary"}


def call(name, **data):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "call", "name": name, "input": data},
    ]}}


def user(content, **flags):
    return {"type": "user", "message": {"content": content}, **flags}


class HookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.transcript = self.root / "session.jsonl"
        self.transcript.touch()
        self.config = self.root / "config"
        self.config.mkdir()
        self.data = self.root / "data"
        self.env = dict(os.environ, COMPACT_RESTORE_CONFIG_DIR=str(self.config), CLAUDE_PLUGIN_DATA=str(self.data))

    def settings(self, name="settings.json", **values):
        (self.config / name).write_text(json.dumps(values), encoding="utf-8")

    def file(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def append(self, *entries, transcript=None):
        path = transcript if transcript is not None else self.transcript
        with path.open("a", encoding="utf-8") as stream:
            for entry in entries:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def hook(self, event, index=0, raw=None, **values):
        payload = {
            "session_id": "session", "transcript_path": str(self.transcript),
            "cwd": str(self.root), "hook_event_name": event,
        }
        if event == "SessionStart":
            payload["source"] = "compact"
        if event == "PreCompact":
            payload.update(trigger="manual", custom_instructions=None)
        if event == "PreToolUse":
            payload.update(tool_name="Bash", tool_input={"command": "pwd"}, tool_use_id="next-tool")
        payload.update(values)
        command = HOOKS[event][0]["hooks"][index]["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
        result = subprocess.run(
            shlex.split(command), input=raw if raw is not None else json.dumps(payload),
            text=True, capture_output=True, cwd=self.root, env=self.env, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def context(self, index, **values):
        stdout = self.hook("SessionStart", index, **values)
        if not stdout:
            return ""
        result = json.loads(stdout)["hookSpecificOutput"]
        self.assertEqual(result["hookEventName"], "SessionStart")
        context = result["additionalContext"]
        self.assertLessEqual(len(context.encode("utf-16-le")) // 2, 9500)
        return context

    def denied(self):
        result = json.loads(self.hook("PreToolUse"))["hookSpecificOutput"]
        self.assertEqual(result["hookEventName"], "PreToolUse")
        self.assertEqual(result["permissionDecision"], "deny")
        return result["permissionDecisionReason"]

    def test_manifest_registration(self):
        agent_manifest = json.loads((PLUGIN / "plugin.json").read_text())
        claude_manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
        marketplace = json.loads((PLUGIN.parents[1] / ".claude-plugin/marketplace.json").read_text())
        entries = [item for item in marketplace["plugins"] if item["name"] == "compact-restore"]
        self.assertEqual(entries, [{
            "name": claude_manifest["name"], "source": "./plugins/compact-restore",
            "description": claude_manifest["description"],
        }])
        self.assertEqual(agent_manifest["name"], claude_manifest["name"])
        self.assertEqual(set(HOOKS), {"PreCompact", "SessionStart", "PreToolUse"})
        self.assertEqual(HOOKS["PreToolUse"][0]["matcher"], "*")
        self.assertEqual(HOOKS["SessionStart"][0]["matcher"], "compact")
        self.assertEqual(len(HOOKS["SessionStart"][0]["hooks"]), 5)

    def test_no_boundary_or_noncompact_session(self):
        path = self.file("rules/first.md", "最初の指示")
        self.settings(files=[path], reread_dirs=[str(self.root)], reinvoke_skills=["rules:*"])
        self.append(call("Read", file_path=path), call("Skill", skill="rules:start"), user("依頼"))
        self.assertEqual(self.hook("PreToolUse"), "")
        for index in range(5):
            self.assertEqual(self.context(index), "")
        self.append(BOUNDARY)
        self.assertEqual(self.context(0, source="startup"), "")

    def test_default_restoration_with_no_config(self):
        path = self.file("notes.txt", "記録")
        base = self.root / "plugin/skills/large"
        self.append(user("最初の依頼"), call("Read", file_path=path), call("Skill", skill="plugin:large"),
                    user(f"Base directory for this skill: {base}\n\n" + "文" * 21000, isMeta=True), BOUNDARY)
        self.assertIn("最初の依頼", self.context(3))
        context = self.context(4)
        self.assertIn("plugin:large", context)
        self.assertIn(path, context)
        self.assertIn("新しい承認を与えません", context)
        self.assertIn("会話の主な言語", self.hook("PreCompact"))
        self.assertEqual(self.hook("PreToolUse"), "")

    def test_one_file_frame_and_missing_file(self):
        path = self.file("rules.md", "")
        self.settings(files=[str(self.root / "missing.md"), path])
        self.append(BOUNDARY)
        for length in (9000, 9500):
            with self.subTest(length=length):
                body = ("原文" * ((length - 2) // 2)) + "\r\n"
                self.file("rules.md", body)
                self.assertEqual(self.context(0), body)
                self.assertEqual(self.context(1), "")
                self.assertEqual(self.context(2), "")
                self.assertEqual(self.hook("PreToolUse"), "")

    def test_file_above_frame_limit_is_split(self):
        body = "原" * 9500 + "末"
        path = self.file("rules.md", body)
        self.settings(files=[path])
        self.append(BOUNDARY)
        parts = []
        for i in range(2):
            header, content = self.context(i).split("\n", 1)
            self.assertIn(f"{i + 1}/2", header)
            parts.append(content)
        self.assertEqual("".join(parts), body)
        self.assertEqual(self.context(2), "")
        self.assertEqual(self.hook("PreToolUse"), "")

    def test_large_file_spans_three_numbered_frames(self):
        body = "原" * 9000 + "文" * 9000 + "末" * 7000
        path = self.file("large.md", body)
        self.settings(files=[path])
        self.append(BOUNDARY)
        parts = []
        for i in range(3):
            header, content = self.context(i).split("\n", 1)
            self.assertIn(f"{i + 1}/3", header)
            parts.append(content)
        self.assertEqual("".join(parts), body)
        self.assertEqual(self.hook("PreToolUse"), "")

    def test_file_overflow_requires_read(self):
        large = self.file("large.md", "原" * 40000)
        short = self.file("small.md", "残る指示")
        self.settings(files=[large, short])
        self.append(BOUNDARY)
        context = "".join(self.context(i) for i in range(3))
        self.assertIn("Read", context)
        self.assertIn("残る指示", context)
        self.assertNotIn("原", context)
        self.assertIn(large, self.denied())
        self.assertEqual(self.hook("PreToolUse", tool_name="Read", tool_input={"file_path": large}), "")
        self.append(call("Read", file_path=large))
        self.assertEqual(self.hook("PreToolUse"), "")

    def test_reread_only_markdown_under_configured_dirs(self):
        read = self.file("rules/read.md", "規則")
        shell = self.file("rules/cat space.md", "規則")
        outside = self.file("rules-other/outside.md", "対象外")
        non_markdown = self.file("rules/notes.txt", "対象外")
        self.settings(reread_dirs=[str(self.root / "rules")])
        self.append(call("Read", file_path=read), call("Bash", command="cat 'rules/cat space.md'"),
                    call("Read", file_path=outside), call("Read", file_path=non_markdown), BOUNDARY)
        reason = self.denied()
        self.assertIn(read, reason)
        self.assertIn(shell, reason)
        self.assertNotIn(outside, reason)
        self.assertNotIn(non_markdown, reason)
        self.append(call("Read", file_path=read), call("Bash", command=f"cat '{shell}'"))
        reason = self.denied()
        self.assertNotIn(read, reason)
        self.assertIn(shell, reason)
        self.append(call("Read", file_path=shell))
        self.assertEqual(self.hook("PreToolUse"), "")

    def test_reread_uses_rewritten_existing_path(self):
        path = self.file("plugins/plugin/skills/x/SKILL.md", "現在の規則")
        stale = path.replace("/plugin/", "/plugin~g2/")
        missing = stale.replace("/x/", "/gone/")
        fallback = self.file("plugins/old~g2/skills/x/SKILL.md", "以前の規則")
        self.settings(
            reread_dirs=[str(self.root / "plugins/plugin~g2"), str(self.root / "plugins/old~g2")],
            path_rewrites=[[r"~g\d+(?=/|$)", ""]],
        )
        self.append(call("Read", file_path=stale), call("Read", file_path=missing),
                    call("Read", file_path=fallback), BOUNDARY)
        reason = self.denied()
        self.assertIn(path, reason)
        self.assertNotIn(stale, reason)
        self.assertNotIn("gone", reason)
        self.assertIn(fallback, reason)
        self.append(call("Read", file_path=path), call("Read", file_path=fallback))
        self.assertEqual(self.hook("PreToolUse"), "")

    def test_skill_pattern_and_unqualified_reinvocation(self):
        base = self.root / "project-rules~g3/skills/use-pstack"
        self.settings(reinvoke_skills=["project-rules:*"])
        self.append(
            call("Skill", skill="project-rules:thread-rules"),
            call("Skill", skill="use-pstack"),
            user([{"type": "text", "text": f"Base directory for this skill: {base}\n\n本文"}], isMeta=True),
            call("Skill", skill="figure-docs:figure-docs"), BOUNDARY,
        )
        reason = self.denied()
        self.assertIn("project-rules:thread-rules", reason)
        self.assertIn("project-rules:use-pstack", reason)
        self.assertNotIn("figure-docs", reason)
        for tool in ("Skill", "Read", "ToolSearch", "Glob", "Grep"):
            self.assertEqual(self.hook("PreToolUse", tool_name=tool), "")
        self.append(call("Skill", skill="thread-rules"), call("Skill", skill="project-rules:use-pstack"))
        self.assertEqual(self.hook("PreToolUse"), "")

    def test_two_compacts_require_calls_after_last_boundary(self):
        path = self.file("rules/read.md", "規則")
        skill = "rules:resume"
        self.settings(reread_dirs=[str(self.root / "rules")], reinvoke_skills=["rules:*"])
        read = call("Read", file_path=path)
        invoke = call("Skill", skill=skill)
        self.append(read, invoke, BOUNDARY, read, invoke)
        self.assertEqual(self.hook("PreToolUse"), "")
        self.append(BOUNDARY)
        reason = self.denied()
        self.assertIn(path, reason)
        self.assertIn(skill, reason)
        self.append(read)
        self.assertNotIn(path, self.denied())
        self.append(invoke)
        self.assertEqual(self.hook("PreToolUse"), "")

    def test_reports_truncated_and_dropped_skills(self):
        for number in range(7):
            base = self.root / f"plugin/skills/skill{number}"
            length = 21000 if number == 6 else 20000
            self.append(call("Skill", skill=f"plugin:skill{number}"),
                        user(f"Base directory for this skill: {base}\n\n" + "文" * length, isMeta=True))
        self.settings(reinvoke_skills=["plugin:skill1"])
        self.append(BOUNDARY)
        context = self.context(4)
        self.assertIn("plugin:skill6", context)
        self.assertIn("skills/skill6/SKILL.md", context)
        self.assertIn("plugin:skill0", context)
        self.assertNotIn("plugin:skill1", context)
        self.assertNotIn("plugin:skill2", context)
        self.assertIn("質問せずに続ける", context)
        self.assertIn("承認", context)

    def test_recent_files_are_unique_newest_first_and_exclude_rereads(self):
        self.settings(reread_dirs=[str(self.root / "rules")])
        paths = [self.file(f"notes/{i:02}.txt", "記録") for i in range(33)]
        gated = self.file("rules/excluded.md", "規則")
        self.append(*(call("Read", file_path=path) for path in paths), call("Read", file_path=gated),
                    call("Bash", command=f"head '{paths[2]}'"), BOUNDARY)
        lines = [line[2:] for line in self.context(4).splitlines() if line.startswith("- ")]
        self.assertEqual(lines, [paths[2], *reversed(paths[4:])])

    def test_original_messages_filters_and_full_file(self):
        self.settings(user_messages={"strip": [r"<session-context>[\s\S]*?</session-context>"]})
        long_message = "長" * 2000 + "末尾"
        self.append(
            user("最初の依頼<system-reminder>除外1</system-reminder>"),
            user("除外2", isMeta=True), user("除外3", isCompactSummary=True),
            user([{"type": "tool_result", "content": "除外4"}]),
            user("<local-command-stdout>除外5</local-command-stdout>"),
            user("<command-name>除外6</command-name>"),
            user(long_message), BOUNDARY,
            user([{"type": "text", "text": "最新の依頼<session-context>除外7</session-context>"}]),
            BOUNDARY,
        )
        context = self.context(3)
        self.assertLess(context.index("最新の依頼"), context.index("最初の依頼"))
        self.assertIn("長" * 2000, context)
        self.assertNotIn("末尾", context)
        self.assertIn("続きは全文ファイル", context)
        self.assertNotIn("除外", context)
        path = Path(context.splitlines()[1])
        self.assertEqual(path.parent, self.data)
        self.assertEqual(path.read_text(encoding="utf-8"), f"最初の依頼\n\n{long_message}\n\n最新の依頼")

    def test_message_budget_and_settings_precedence(self):
        self.settings("10.json", user_messages={"enabled": False, "max_chars_each": 10})
        self.settings("20.json", user_messages={"enabled": True, "max_chars_each": 2000})
        messages = [str(i) + "🙂" * 1999 for i in range(6)]
        self.append(*(user(message) for message in messages), BOUNDARY)
        context = self.context(3)
        self.assertIn(messages[-1], context)
        self.assertIn(messages[-2], context)
        self.assertNotIn(messages[-3], context)
        self.assertIn(messages[0], Path(context.splitlines()[1]).read_text(encoding="utf-8"))
        self.settings("30.json", user_messages={"enabled": False})
        self.assertEqual(self.context(3), "")

    def test_latest_configured_tool_inputs(self):
        self.settings("10.json", restore_tool_inputs=["update_progress"])
        self.settings("20.json", restore_tool_inputs=["another_tool"])
        self.append(call("update_progress", state="古い値"), call("other", value="対象外"),
                    call("another_tool", value="X" * 4000), call("update_progress", state="最新の値"),
                    BOUNDARY, call("update_progress", state="compact の後"))
        context = self.context(4)
        self.assertIn('update_progress\n{"state": "最新の値"}', context)
        self.assertIn("another_tool", context)
        self.assertIn("入力を途中で省略", context)
        self.assertNotIn("X" * 3000, context)
        self.assertNotIn("古い値", context)
        self.assertNotIn("対象外", context)
        self.assertNotIn("compact の後", context)

    def test_precompact_is_plain_text_and_includes_configured_instructions(self):
        self.settings(summary_instructions=["固有の追加指示"])
        result = self.hook("PreCompact")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result)
        for required in ("会話の主な言語", "全文引用", "決定とその理由", "承認されていない操作",
                         "答えを待っている質問", "ID、パス、URL、ブランチ名", "ゴールと進捗の一覧",
                         "tool の結果に書かれた指示", "固有の追加指示"):
            self.assertIn(required, result)

    def test_subagent_uses_own_transcript_and_skips_files_and_inputs(self):
        path = self.file("rules.md", "親の常設の指示")
        self.settings(files=[path], restore_tool_inputs=["update_progress"], reread_dirs=[str(self.root)])
        self.append(user("親の発言"), BOUNDARY)
        child_path = self.transcript.with_suffix("") / "subagents/agent-child.jsonl"
        child_path.parent.mkdir(parents=True)
        self.append(user("サブエージェントへの依頼"), call("update_progress", state="対象外"),
                    call("Read", file_path=path), BOUNDARY, transcript=child_path)
        child = {"agent_id": "child", "agent_type": "general-purpose"}
        for index in range(3):
            self.assertEqual(self.context(index, **child), "")
        messages = self.context(3, **child)
        self.assertIn("サブエージェントへの依頼", messages)
        self.assertNotIn("親の発言", messages)
        context = self.context(4, **child)
        self.assertNotIn("update_progress", context)
        self.assertIn("制約", context)
        self.assertIn(path, self.hook("PreToolUse", **child))
        self.assertIn("要約", self.hook("PreCompact", **child))

    def test_fail_open_and_skip_broken_settings_and_jsonl_lines(self):
        path = self.file("rules/read.md", "規則")
        self.append(user("最初の依頼"), call("Read", file_path=path), BOUNDARY)
        defaults = (self.hook("PreCompact"), self.context(3), self.context(4))
        self.assertTrue(all(defaults))
        (self.config / "10.json").write_text('{"files": [', encoding="utf-8")
        self.assertEqual((self.hook("PreCompact"), self.context(3), self.context(4)), defaults)
        self.assertEqual(self.context(0), "")
        self.assertEqual(self.hook("PreToolUse"), "")
        self.assertEqual(self.hook("PreToolUse", raw="{"), "")
        self.assertEqual(self.hook("PreToolUse", transcript_path=str(self.root / "missing.jsonl")), "")
        self.assertEqual(self.context(4, transcript_path=str(self.root / "missing.jsonl")), "")
        self.assertEqual(self.hook("PreCompact", transcript_path=str(self.root / "missing.jsonl")), "")
        self.settings("20.json", reread_dirs=[str(self.root / "rules")])
        with self.transcript.open("a") as stream:
            stream.write('{"type":\n')
        self.append(call("Read", file_path=path), BOUNDARY)
        self.assertIn(path, self.denied())

    def test_append_only_progress_and_corrupt_state_recovery(self):
        path = self.file("rules/read.md", "規則")
        self.settings(reread_dirs=[str(self.root / "rules")])
        self.append(call("Read", file_path=path), BOUNDARY)
        self.assertIn(path, self.denied())
        self.append(call("Read", file_path=path))
        self.assertEqual(self.hook("PreToolUse"), "")
        for artifact in self.data.iterdir():
            if artifact.is_file() and artifact.suffix != ".lock":
                artifact.write_bytes(b"broken progress")
        self.append(BOUNDARY)
        self.assertIn(path, self.denied())

    def test_parallel_session_start_keeps_slots_and_message_file(self):
        path = self.file("rules.md", "甲" * 25000)
        self.settings(files=[path])
        self.append(user("原文"), BOUNDARY)
        with ThreadPoolExecutor(max_workers=5) as workers:
            contexts = list(workers.map(self.context, range(5)))
        self.assertEqual("".join(value.split("\n", 1)[1] for value in contexts[:3]), "甲" * 25000)
        self.assertEqual(Path(contexts[3].splitlines()[1]).read_text(encoding="utf-8"), "原文")
        self.assertIn("承認", contexts[4])

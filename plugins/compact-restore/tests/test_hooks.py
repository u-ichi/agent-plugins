import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[1]
HOOKS = json.loads((PLUGIN / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
BOUNDARY = {"type": "system", "subtype": "compact_boundary"}


def tool(name, **data):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tool-1", "name": name, "input": data},
    ]}}


def user(content, **flags):
    return {"type": "user", "message": {"content": content}, **flags}


def skill(name, directory=None, body="本文"):
    text = f"Base directory for this skill: {directory}\n\n{body}" if directory else body
    return [tool("Skill", skill=name), user([{"type": "text", "text": text}], isMeta=True)]


class HookTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        self.data_dir = self.root / "data"
        self.transcript = self.root / "session.jsonl"
        self.transcript.touch()
        self.env = dict(
            os.environ,
            COMPACT_RESTORE_CONFIG_DIR=str(self.config_dir),
            CLAUDE_PLUGIN_DATA=str(self.data_dir),
        )

    def file(self, relative, contents="本文"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def config(self, name="default", **value):
        self.file(f"config/{name}.json", json.dumps(value, ensure_ascii=False))

    def append(self, *rows, path=None):
        with (path or self.transcript).open("a", encoding="utf-8") as output:
            for row in rows:
                output.write((row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)) + "\n")

    def run_hook(self, event, index=0, raw=None, **overrides):
        registration = HOOKS[event][0]
        command = registration["hooks"][index]["command"]
        command = shlex.split(command.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN)))
        payload = {
            "session_id": "session-1",
            "transcript_path": str(self.transcript),
            "cwd": str(self.root),
            "hook_event_name": event,
        }
        if event == "SessionStart":
            payload["source"] = "compact"
        elif event == "PreCompact":
            payload.update(trigger="auto", custom_instructions=None)
        else:
            payload.update(tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="t1")
        payload.update(overrides)
        process = subprocess.run(
            command, input=json.dumps(payload) if raw is None else raw, env=self.env,
            capture_output=True, encoding="utf-8", timeout=15, cwd=self.root,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        return process.stdout

    def context(self, index, **overrides):
        stdout = self.run_hook("SessionStart", index, **overrides)
        if not stdout:
            return ""
        context = json.loads(stdout)["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "SessionStart")
        text = context["additionalContext"]
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 9500)
        return text

    def deny(self, **overrides):
        stdout = self.run_hook("PreToolUse", **overrides)
        context = json.loads(stdout)["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "PreToolUse")
        self.assertEqual(context["permissionDecision"], "deny")
        return context["permissionDecisionReason"]

    def test_hook_registration(self):
        self.assertEqual(set(HOOKS), {"PreCompact", "SessionStart", "PreToolUse"})
        self.assertEqual(HOOKS["SessionStart"][0]["matcher"], "compact")
        self.assertEqual(len(HOOKS["SessionStart"][0]["hooks"]), 5)
        self.assertEqual(HOOKS["PreToolUse"][0]["matcher"], "*")
        self.assertEqual(self.run_hook("SessionStart", 4, source="startup"), "")

    def test_no_boundary_leaves_tools_available(self):
        document = self.file("rules/a.md")
        self.config(reread_dirs=[str(document.parent)], reinvoke_skills=["project:*"])
        self.append(tool("Read", file_path=str(document)), *skill("project:x"))
        self.assertEqual(self.run_hook("PreToolUse"), "")
        details = self.context(4)
        self.assertIn("承認", details)
        self.assertNotIn(str(document), details)
        self.assertNotIn("project:x", details)

    def test_files_single_slot_and_missing_file(self):
        body = "0123456789" * 900
        instruction = self.file("instructions.md", body)
        missing = self.root / "missing.md"
        self.config(files=[str(missing), str(instruction)])
        self.append(BOUNDARY)
        context = self.context(0)
        self.assertTrue(context.endswith(body))
        self.assertNotIn(str(missing), context)
        self.assertEqual(self.context(1), "")
        self.assertEqual(self.context(2), "")
        self.assertEqual(self.run_hook("PreToolUse"), "")

    def test_files_split_across_three_numbered_slots(self):
        body = "".join(str(number % 10) for number in range(25000))
        instruction = self.file("long.md", body)
        self.config(files=[str(instruction)])
        self.append(BOUNDARY)
        parts = []
        for index in range(3):
            context = self.context(index)
            heading, content = context.split("\n\n", 1)
            self.assertIn(f"{index + 1}/3", heading)
            if index == 0:
                self.assertIn(str(instruction), content.splitlines()[0])
                content = content.split("\n", 1)[1]
            parts.append(content)
        self.assertEqual("".join(parts), body)
        self.assertEqual(self.run_hook("PreToolUse"), "")

    def test_files_overflow_requires_read(self):
        long = self.file("long.md", "Z" * 40000)
        short = self.file("short.md", "短い指示")
        self.config(files=[str(short), str(long)])
        self.append(BOUNDARY)
        context = self.context(0)
        self.assertIn("枠に収まらなかった常設の指示は Read", context)
        self.assertIn("短い指示", context)
        self.assertNotIn("ZZZZ", context)
        self.assertEqual(self.context(1), "")
        self.assertEqual(self.context(2), "")
        self.assertIn(str(long), self.deny())
        self.assertEqual(self.run_hook("PreToolUse", tool_name="Read"), "")
        self.append(tool("Read", file_path=str(long)))
        self.assertEqual(self.run_hook("PreToolUse"), "")

    def test_reread_uses_read_and_bash_paths(self):
        read = self.file("rules/nested/a.md")
        cat = self.file("rules/foo bar.md")
        outside = self.file("outside/a.md")
        other_type = self.file("rules/file.txt")
        outside_sibling = self.file("rules-other/extra.md")
        gone = self.root / "rules/gone.md"
        self.config(reread_dirs=[str(self.root / "rules")])
        self.append(
            tool("Read", file_path=str(read)),
            tool("Bash", command='cat "rules/foo bar.md" | head -n 3'),
            tool("Read", file_path=str(outside)),
            tool("Read", file_path=str(other_type)),
            tool("Read", file_path=str(outside_sibling)),
            tool("Read", file_path=str(gone)),
            BOUNDARY,
        )
        reason = self.deny()
        self.assertIn(str(read), reason)
        self.assertIn(str(cat), reason)
        for path in (outside, other_type, outside_sibling, gone):
            self.assertNotIn(str(path), reason)
        self.append(tool("Read", file_path=str(read)), tool("Bash", command=f"cat '{cat}'"))
        self.assertNotIn(str(read), self.deny())
        self.assertIn(str(cat), self.deny())
        self.append(tool("Read", file_path=str(cat)))
        self.assertEqual(self.run_hook("PreToolUse"), "")

    def test_path_rewrites_apply_to_history_and_read(self):
        new = self.file("plugin/skills/x/SKILL.md")
        old = self.root / "plugin~g2/skills/x/SKILL.md"
        self.config(reread_dirs=[str(self.root / "plugin")], path_rewrites=[[r"~g\d+(?=/)", ""]])
        self.append(tool("Read", file_path=str(old)), BOUNDARY)
        reason = self.deny()
        self.assertIn(str(new), reason)
        self.assertNotIn(str(old), reason)
        self.append(tool("Read", file_path=str(new)))
        self.assertEqual(self.run_hook("PreToolUse"), "")

    def test_reinvoke_skill_matches_qualified_names(self):
        self.config(reinvoke_skills=["project-rules:*"])
        rules = self.root / "project-rules~g3"
        self.append(
            *skill("project-rules:thread-rules", rules / "skills/thread-rules"),
            *skill("use-pstack", rules / "skills/use-pstack"),
            *skill("figure-docs:figure-docs"),
            BOUNDARY,
        )
        reason = self.deny()
        self.assertIn("project-rules:thread-rules", reason)
        self.assertIn("project-rules:use-pstack", reason)
        self.assertNotIn("figure-docs", reason)
        for name in ("Skill", "Read", "ToolSearch", "Glob", "Grep"):
            self.assertEqual(self.run_hook("PreToolUse", tool_name=name), "")
        self.append(*skill("thread-rules", rules / "skills/thread-rules"))
        self.assertNotIn("thread-rules", self.deny())
        self.assertIn("use-pstack", self.deny())
        self.append(tool("Skill", skill="project-rules:use-pstack"))
        self.assertEqual(self.run_hook("PreToolUse"), "")

    def test_latest_boundary_resets_rereads_and_reinvocations(self):
        first = self.file("rules/first.md")
        second = self.file("rules/second.md")
        self.config(reread_dirs=[str(first.parent)], reinvoke_skills=["project:*"])
        self.append(
            tool("Read", file_path=str(first)), tool("Skill", skill="project:old"),
            BOUNDARY,
            tool("Read", file_path=str(first)), tool("Read", file_path=str(second)),
            tool("Skill", skill="project:old"), tool("Skill", skill="project:new"),
            BOUNDARY,
            tool("Read", file_path=str(second)), tool("Skill", skill="project:new"),
        )
        reason = self.deny()
        self.assertIn(str(first), reason)
        self.assertNotIn(str(second), reason)
        self.assertIn("project:old", reason)
        self.assertNotIn("project:new", reason)

    def test_skill_notices_include_truncated_and_dropped_skills(self):
        directory = self.root / "plugins/example/skills"
        for index, length in enumerate([1000, 21001, 20000, 20000, 20000, 20000]):
            name = f"skill-{index}"
            self.append(*skill("example:" + name, directory / name, "x" * length))
        self.append(BOUNDARY)
        context = self.context(4)
        for index in (0, 1):
            name = f"skill-{index}"
            self.assertIn("example:" + name, context)
            self.assertIn(str(directory / name / "SKILL.md"), context)
        for index in range(2, 6):
            self.assertNotIn(f"example:skill-{index}", context)
        self.config(reinvoke_skills=["example:skill-1"])
        self.assertNotIn("example:skill-1", self.context(4))

    def test_file_notices_list_recent_paths_outside_reread_dirs(self):
        paths = [self.file(f"files/{index:02d}.txt") for index in range(31)]
        required = self.file("rules/required.md")
        self.config(reread_dirs=[str(required.parent)])
        self.append(*(tool("Read", file_path=str(path)) for path in paths))
        self.append(tool("Read", file_path=str(required)), BOUNDARY)
        context = self.context(4)
        self.assertNotIn(str(required), context)
        self.assertNotIn(str(paths[0]), context)
        self.assertEqual(
            [line for line in context.splitlines() if line.startswith(str(self.root))],
            [str(path) for path in reversed(paths[1:])],
        )
        self.assertIn("Read で読み直して", context)

    def test_user_messages_filter_and_restore_originals(self):
        self.config(user_messages={"strip": [r"<session-context[\s\S]*?</session-context[^>]*>"]})
        self.append(
            user("最初の指示です。\n改行も保存する"),
            user("meta-omit", isMeta=True),
            user("summary-omit", isCompactSummary=True),
            user([{"type": "tool_result", "content": "result-omit"}]),
            user("<local-command-stdout>shell-omit</local-command-stdout>"),
            user("<command-name>command-omit</command-name>"),
            "{broken transcript row",
            user([{"type": "text", "text": "<system-reminder>system-omit</system-reminder>"},
                  {"type": "text", "text": "最新の発言です。<session-context>strip-omit</session-context>"}]),
            BOUNDARY,
        )
        context = self.context(3)
        self.assertLess(context.index("最新の発言です。"), context.index("最初の指示です。"))
        self.assertIn("最初の指示です。\n改行も保存する", context)
        archive_path = Path(re.search(r"ユーザーの発言の全文は (.*?) にあります", context)[1])
        self.assertEqual(archive_path.parent.parent, self.data_dir)
        archive = archive_path.read_text(encoding="utf-8")
        self.assertIn("最初の指示です。\n改行も保存する", archive)
        self.assertIn("最新の発言です。", archive)
        self.assertNotIn("-omit", context)
        self.assertNotIn("-omit", archive)

    def test_user_messages_fit_the_slot_and_archive_keeps_full_text(self):
        large = "😀" * 3000 + "末尾を保存"
        self.append(*(user(f"開始 {index}\n{large}") for index in range(12)), BOUNDARY)
        context = self.context(3)
        self.assertIn("開始 11", context)
        self.assertNotIn("開始 0\n", context)
        self.assertIn("この発言の残り", context)
        self.assertNotIn("末尾を保存", context)
        archive = Path(re.search(r"ユーザーの発言の全文は (.*?) にあります", context)[1])
        self.assertEqual(archive.read_text(encoding="utf-8").count(large), 12)

    def test_last_inputs_and_approval_scope(self):
        name = "mcp__example__update_progress"
        self.config(restore_tool_inputs=[name, "large_tool"])
        self.append(
            tool(name, steps=["古い一覧"]),
            tool("ignored", steps=["ignored-input"]),
            tool(name, steps=["最新の一覧", "完了"]),
            tool("large_tool", text="A" * 4000 + "hidden-tail"),
            BOUNDARY,
            tool(name, steps=["境目の後"]),
        )
        context = self.context(4)
        self.assertIn(name, context)
        self.assertIn(json.dumps({"steps": ["最新の一覧", "完了"]}, ensure_ascii=False), context)
        self.assertIn("A" * 2900, context)
        self.assertNotIn("A" * 3001, context)
        for text in ("古い一覧", "ignored-input", "hidden-tail", "境目の後"):
            self.assertNotIn(text, context)
        self.assertIn("承認、その範囲、制約はそのまま有効", context)
        self.assertIn("「質問せずに続ける」は新しい承認を与えません", context)

    def test_precompact_plain_text_and_configuration_merge(self):
        self.config("a", summary_instructions=["独自の指示 A"], user_messages={
            "enabled": False, "max_chars_each": 2, "strip": ["REMOVE_A"],
        })
        self.config("z", summary_instructions=["独自の指示 Z"], user_messages={
            "enabled": True, "max_chars_each": 8, "strip": ["REMOVE_Z"],
        })
        self.file("config/malformed.json", "{malformed")
        self.append(user("REMOVE_AREMOVE_Zabcdefghijk"), BOUNDARY)
        for trigger in ("manual", "auto"):
            instructions = self.run_hook("PreCompact", trigger=trigger)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(instructions)
            for text in (
                "主に使われている言語", "元の言語で原文のまま引用", "決定とその理由",
                "承認の範囲", "承認されていない操作", "答えを待っている質問",
                "ID、パス、URL、ブランチ名", "ゴールと進捗", "ユーザーの指示として書かない",
            ):
                self.assertIn(text, instructions)
            self.assertLess(instructions.index("独自の指示 A"), instructions.index("独自の指示 Z"))
        context = self.context(3)
        self.assertIn("abcdefgh", context)
        self.assertNotIn("abcdefghi", context)
        self.assertNotIn("REMOVE", context)

    def test_agent_uses_its_own_transcript_and_skips_files_and_inputs(self):
        document = self.file("instructions.md", "親だけの指示")
        agent_rule = self.file("rules/agent.md")
        self.config(
            files=[str(document)], restore_tool_inputs=["update"], reread_dirs=[str(agent_rule.parent)],
            reinvoke_skills=["agent-project:*"],
        )
        self.append(user("親セッションの発言"), BOUNDARY)
        agent = self.transcript.with_suffix("") / "subagents/agent-test.jsonl"
        agent.parent.mkdir(parents=True)
        self.append(
            user("サブエージェントへの依頼の原文"), tool("Read", file_path=str(agent_rule)),
            tool("Skill", skill="agent-project:instructions"), tool("update", data="代理の進捗"),
            BOUNDARY, path=agent,
        )
        args = {"agent_id": "test", "agent_type": "worker"}
        self.assertEqual(self.context(0, **args), "")
        context = self.context(3, **args)
        self.assertIn("サブエージェントへの依頼の原文", context)
        self.assertNotIn("親セッションの発言", context)
        self.assertNotIn("代理の進捗", self.context(4, **args))
        self.assertIn("承認", self.context(4, **args))
        self.assertIn(str(agent_rule), self.deny(**args))
        self.assertIn("agent-project:instructions", self.deny(**args))
        self.assertIn("進捗", self.run_hook("PreCompact", **args))
        self.assertIn("親セッションの発言", self.context(3))

    def test_bad_input_and_missing_transcript_fail_open(self):
        self.append(BOUNDARY)
        for event in ("PreCompact", "SessionStart", "PreToolUse"):
            self.assertEqual(self.run_hook(event, raw="{broken"), "")
            self.assertEqual(self.run_hook(event, transcript_path=str(self.root / "missing")), "")

    def test_malformed_config_keeps_defaults(self):
        document = self.file("reference.md")
        directory = self.root / "example/skills/long"
        self.append(
            user("設定なしでも戻る原文。"),
            tool("Read", file_path=str(document)),
            *skill("example:long", directory, "x" * 21000),
            BOUNDARY,
        )
        summary = self.run_hook("PreCompact")
        messages = self.context(3)
        details = self.context(4)
        self.assertIn("元の言語で原文のまま引用", summary)
        self.assertIn("設定なしでも戻る原文。", messages)
        self.assertIn("example:long", details)
        self.assertIn(str(document), details)
        self.assertIn("承認、その範囲、制約はそのまま有効", details)

        self.file("config/broken.json", "{broken")
        self.assertEqual(self.run_hook("PreCompact"), summary)
        self.assertEqual(self.context(3), messages)
        self.assertEqual(self.context(4), details)
        self.assertEqual(self.run_hook("PreToolUse"), "")
        self.assertEqual(self.context(0), "")

    def test_gate_reads_appended_records_and_recovers_from_damaged_cache(self):
        document = self.file("rules/a.md")
        self.config(reread_dirs=[str(document.parent)])
        self.append(tool("Read", file_path=str(document)), BOUNDARY)
        self.assertIn(str(document), self.deny())
        with self.transcript.open("a", encoding="utf-8") as output:
            record = json.dumps(tool("Read", file_path=str(document)))
            cutoff = len(record) // 2
            output.write(record[:cutoff])
        self.assertIn(str(document), self.deny())
        with self.transcript.open("a", encoding="utf-8") as output:
            output.write(record[cutoff:] + "\n")
        self.assertEqual(self.run_hook("PreToolUse"), "")
        for cache in self.data_dir.glob("*/*.json"):
            cache.write_text("{broken cache", encoding="utf-8")
        self.append(BOUNDARY)
        self.assertIn(str(document), self.deny())


if __name__ == "__main__":
    unittest.main()

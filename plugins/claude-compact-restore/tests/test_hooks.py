import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


PLUGIN = Path(__file__).resolve().parents[1]
BOUNDARY = {"type": "system", "subtype": "compact_boundary"}


def calls(*names):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Skill", "input": {"skill": name}}
                for name in names
            ]
        },
    }


def attached(*names):
    return {
        "type": "attachment",
        "attachment": {
            "type": "invoked_skills",
            "skills": [{"name": name, "content": "Example", "path": "SKILL.md"} for name in names],
        },
    }


class HookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.instructions = self.home / "instructions.txt"
        self.transcript = self.home / "transcript.jsonl"
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("CLAUDE_COMPACT_RESTORE_")
        }
        self.env.update(HOME=str(self.home), CLAUDE_PLUGIN_ROOT=str(PLUGIN))

    def run_hook(self, command, event):
        arguments = shlex.split(command)
        arguments = [
            arg.replace("${CLAUDE_PLUGIN_ROOT}", self.env["CLAUDE_PLUGIN_ROOT"])
            for arg in arguments
        ]
        result = subprocess.run(
            arguments,
            input=json.dumps(event),
            capture_output=True,
            encoding="utf-8",
            env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)["hookSpecificOutput"] if result.stdout else None

    def pre_tool_use(self, rows=None, **overrides):
        if rows is not None:
            self.transcript.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
        event = {"tool_name": "Read", "transcript_path": str(self.transcript), **overrides}
        [group] = self.hooks["PreToolUse"]
        self.assertEqual(group["matcher"], "*")
        [hook] = group["hooks"]
        return self.run_hook(hook["command"], event)

    def session_start(self, **overrides):
        event = {"source": "compact", **overrides}
        [group] = self.hooks["SessionStart"]
        self.assertEqual(group["matcher"], "compact")
        first, second = group["hooks"]
        return tuple(self.run_hook(hook["command"], event) for hook in (first, second))

    def configure_instructions(self, content):
        self.env["CLAUDE_COMPACT_RESTORE_INSTRUCTIONS"] = "~/instructions.txt"
        self.instructions.write_bytes(content.encode("utf-8"))

    def assert_denied_for(self, output, name):
        self.assertIsNotNone(output)
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        reason = output["permissionDecisionReason"]
        for fragment in ("compact", "Claude Code", "付け直さなかった", "Skill", "呼び直し", name):
            self.assertIn(fragment, reason)
        return reason

    def test_denies_only_missing_skill_until_recalled(self):
        self.env["CLAUDE_COMPACT_RESTORE_SKILLS"] = "orchard:*"
        rows = [calls("orchard:prune", "orchard:harvest"), BOUNDARY, attached("orchard:harvest")]
        reason = self.assert_denied_for(self.pre_tool_use(rows), "orchard:prune")
        self.assertNotIn("orchard:harvest", reason)
        self.assertIsNone(self.pre_tool_use(rows + [calls("orchard:prune")]))

    def test_all_skills_reattached(self):
        self.env["CLAUDE_COMPACT_RESTORE_SKILLS"] = "*"
        self.assertIsNone(self.pre_tool_use([
            calls("orchard:prune", "orchard:harvest"), BOUNDARY,
            attached("orchard:prune", "orchard:harvest"),
        ]))

    def test_uses_last_compact_boundary(self):
        self.env["CLAUDE_COMPACT_RESTORE_SKILLS"] = "*"
        rows = [
            calls("orchard:prune"), BOUNDARY, attached("orchard:prune"),
            calls("orchard:prune", "orchard:graft", "orchard:harvest"),
            BOUNDARY, attached("orchard:harvest"),
        ]
        reason = self.assert_denied_for(self.pre_tool_use(rows), "orchard:prune")
        self.assertIn("orchard:graft", reason)
        self.assertNotIn("orchard:harvest", reason)

    def test_no_compact_boundary(self):
        self.env["CLAUDE_COMPACT_RESTORE_SKILLS"] = "*"
        self.assertIsNone(self.pre_tool_use([calls("orchard:prune")]))

    def test_skill_filters_and_hook_guards(self):
        rows = [calls("orchard:prune"), BOUNDARY]
        self.assertIsNone(self.pre_tool_use(rows))
        self.env["CLAUDE_COMPACT_RESTORE_SKILLS"] = "orchard:harvest"
        self.assertIsNone(self.pre_tool_use(rows))
        self.env["CLAUDE_COMPACT_RESTORE_SKILLS"] = "orchard:harvest,orchard:prune"
        self.assert_denied_for(self.pre_tool_use(rows), "orchard:prune")
        for overrides in ({"agent_id": "worker"}, {"tool_name": "Skill"}, {"tool_name": "ToolSearch"}):
            with self.subTest(overrides=overrides):
                self.assertIsNone(self.pre_tool_use(rows, **overrides))

    def test_matches_qualified_and_short_skill_names(self):
        self.env["CLAUDE_COMPACT_RESTORE_SKILLS"] = "prune"
        rows = [calls("garden:orchard:prune"), BOUNDARY]
        self.assert_denied_for(self.pre_tool_use(rows), "garden:orchard:prune")
        self.assertIsNone(self.pre_tool_use(rows + [attached("prune")]))
        self.assertIsNone(self.pre_tool_use(rows + [calls("prune")]))
        self.assertIsNone(self.pre_tool_use([calls("prune"), BOUNDARY, attached("orchard:prune")]))

    def test_different_plugins_with_the_same_skill_name(self):
        self.env["CLAUDE_COMPACT_RESTORE_SKILLS"] = "a:*"
        rows = [calls("a:review"), BOUNDARY]
        for restored in (attached, calls):
            with self.subTest(restored=restored.__name__):
                reason = self.assert_denied_for(self.pre_tool_use(rows + [restored("b:review")]), "a:review")
                self.assertNotIn("b:review", reason)
                self.assertIsNone(self.pre_tool_use(rows + [restored("review")]))

        self.env["CLAUDE_COMPACT_RESTORE_SKILLS"] = "*"
        reason = self.assert_denied_for(self.pre_tool_use([calls("a:review", "b:review"), BOUNDARY]), "a:review")
        self.assertIn("b:review", reason)

    def test_unreadable_transcript_or_broken_candidate_line(self):
        self.env["CLAUDE_COMPACT_RESTORE_SKILLS"] = "*"
        self.assertIsNone(self.pre_tool_use())
        rows = [calls("orchard:prune"), BOUNDARY]
        self.assert_denied_for(self.pre_tool_use(rows), "orchard:prune")
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"attachment","attachment":{"type":"invoked_skills"\n')
        self.assertIsNone(self.pre_tool_use())

    def test_short_instructions_preserve_whole_file(self):
        text = "  指示一\r\n指示二\r\n"
        self.configure_instructions(text)
        first, second = self.session_start()
        self.assertEqual(first["hookEventName"], "SessionStart")
        context = first["additionalContext"]
        self.assertTrue(context.endswith(text))
        for fragment in ("compact", "プロジェクト指示の全文", "プロジェクト指示は要約の言い換え", "決まったこと", "依頼"):
            self.assertIn(fragment, context[:-len(text)])
        self.assertIsNone(second)

    def test_instructions_split_at_line_boundary_by_utf16_units(self):
        lines = [("庭🌱" * 1500) + "\r\n", ("畑🍅" * 1500) + "\n"]
        text = "".join(lines)
        self.assertLess(len(text), 9000)
        self.configure_instructions(text)
        first, second = self.session_start()
        for part, line in zip((first, second), lines):
            self.assertEqual(part["hookEventName"], "SessionStart")
            context = part["additionalContext"]
            self.assertTrue(context.endswith(line))
            self.assertEqual(context.count(line), 1)
            self.assertLessEqual(len(context.encode("utf-16-le")) // 2, 9000)
        self.assertNotIn(lines[1], first["additionalContext"])
        self.assertNotIn(lines[0], second["additionalContext"])
        self.assertIn("プロジェクト指示の続き", second["additionalContext"][:-len(lines[1])])

    def test_instructions_fit_at_exact_limit(self):
        self.configure_instructions("x")
        first, _ = self.session_start()
        prefix_units = len(first["additionalContext"][:-1].encode("utf-16-le")) // 2
        text = "x" * (9000 - prefix_units)
        self.configure_instructions(text)
        first, second = self.session_start()
        context = first["additionalContext"]
        self.assertTrue(context.endswith(text))
        self.assertEqual(len(context.encode("utf-16-le")) // 2, 9000)
        self.assertIsNone(second)

    def test_instructions_fallback_if_two_parts_cannot_fit(self):
        for text in ("庭\n" * 9000, "庭" * 9000):
            with self.subTest(lines=text.count("\n")):
                self.configure_instructions(text)
                first, second = self.session_start()
                self.assertEqual(first["hookEventName"], "SessionStart")
                reason = first["additionalContext"]
                self.assertIn(f"プロジェクト指示を書いたファイル {self.instructions} を Read で読み直してから続け", reason)
                self.assertEqual(len(reason.splitlines()), 1)
                self.assertNotIn("庭", reason)
                self.assertIsNone(second)

    def test_instruction_guards_and_missing_or_empty_file(self):
        self.env["CLAUDE_COMPACT_RESTORE_INSTRUCTIONS"] = str(self.instructions)
        self.assertEqual(self.session_start(), (None, None))
        self.configure_instructions("")
        self.assertEqual(self.session_start(), (None, None))
        self.configure_instructions("指示")
        self.assertEqual(self.session_start(source="resume"), (None, None))
        self.assertEqual(self.session_start(agent_id="worker"), (None, None))

    def test_config_paths_and_environment_precedence_per_item(self):
        self.instructions.write_text("設定の指示", encoding="utf-8")
        config = {"instructions": "~/instructions.txt", "skills": ["orchard:prune"]}
        config_path = self.home / ".claude" / "claude-compact-restore.json"
        config_path.parent.mkdir()
        config_path.write_text(json.dumps(config), encoding="utf-8")
        first, _ = self.session_start()
        self.assertTrue(first["additionalContext"].endswith("設定の指示"))
        rows = [calls("orchard:prune", "orchard:harvest"), BOUNDARY]
        self.assert_denied_for(self.pre_tool_use(rows), "orchard:prune")

        custom_path = self.home / "settings.json"
        self.env["CLAUDE_COMPACT_RESTORE_CONFIG"] = "~/settings.json"
        config["skills"] = ["orchard:harvest"]
        custom_path.write_text(json.dumps(config), encoding="utf-8")
        reason = self.assert_denied_for(self.pre_tool_use(rows), "orchard:harvest")
        self.assertNotIn("orchard:prune", reason)
        override = self.home / "override.txt"
        override.write_text("環境変数の指示", encoding="utf-8")
        self.env["CLAUDE_COMPACT_RESTORE_INSTRUCTIONS"] = str(override)
        first, _ = self.session_start()
        self.assertTrue(first["additionalContext"].endswith("環境変数の指示"))
        self.assert_denied_for(self.pre_tool_use(rows), "orchard:harvest")
        self.env["CLAUDE_COMPACT_RESTORE_SKILLS"] = "orchard:prune"
        reason = self.assert_denied_for(self.pre_tool_use(rows), "orchard:prune")
        self.assertNotIn("orchard:harvest", reason)

    def test_broken_config_or_wrong_types(self):
        config_path = self.home / "settings.json"
        self.env["CLAUDE_COMPACT_RESTORE_CONFIG"] = str(config_path)
        rows = [calls("orchard:prune"), BOUNDARY]
        for config in ('{"instructions":', "[]", '{"instructions": 7, "skills": ["*", 7]}',
                       '{"instructions": [], "skills": "*"}'):
            with self.subTest(config=config):
                config_path.write_text(config, encoding="utf-8")
                self.assertEqual(self.session_start(), (None, None))
                self.assertIsNone(self.pre_tool_use(rows))

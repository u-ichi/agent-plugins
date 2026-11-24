import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


PLUGIN = Path(__file__).resolve().parents[1]
BOUNDARY = {"type": "system", "subtype": "compact_boundary"}
REPLY = "mcp__hearthbot__message_coordinator"
POST_MESSAGE = "mcp__hearthbot__post_message"
POST_WIDGET = "mcp__hearthbot__post_widget"


def calls(*names):
    return {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": name}}
            for name in names
        ]},
    }


def attachment(kind, **fields):
    return {"type": "attachment", "attachment": {"type": kind, **fields}}


def attached(*names):
    return attachment("invoked_skills", skills=[{"name": name, "path": "SKILL.md"} for name in names])


def user(content, **fields):
    return {"type": "user", "message": {"content": content}, **fields}


def instruction_block(text):
    return f'<project-instructions nonce="0a9F" source="project">{text}</project-instructions nonce="0a9F">'


class HookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        self.transcript = self.home / "transcript.jsonl"
        self.state = self.home / "state"
        self.plugins = self.home / ".claude/plugins"
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("CLAUDE_COMPACT_RESTORE_") and key != "CLAUDE_CODE_PROJECTS_SESSION"
        }
        self.env.update(HOME=str(self.home), CLAUDE_PLUGIN_ROOT=str(PLUGIN),
                        CLAUDE_COMPACT_RESTORE_STATE_DIR=str(self.state))

    def run_hook(self, hook, registered, **overrides):
        event = {
            "hook_event_name": hook, "session_id": "session",
            "transcript_path": str(self.transcript), "cwd": str(self.home), **overrides,
        }
        command = registered["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
        result = subprocess.run(shlex.split(command), input=json.dumps(event), capture_output=True,
                                encoding="utf-8", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)["hookSpecificOutput"] if result.stdout else None

    def run_one(self, hook, **event):
        [group] = self.hooks[hook]
        [registered] = group["hooks"]
        return self.run_hook(hook, registered, **event)

    def write_transcript(self, *rows):
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def pre(self, tool="Read", **overrides):
        return self.run_one("PreToolUse", tool_name=tool, **overrides)

    def post(self, tool, args, response=None, **overrides):
        return self.run_one("PostToolUse", tool_name=tool, tool_input=args,
                            tool_response=response, **overrides)

    def post_read(self, path, response=None, offset=None, **event):
        args = {"file_path": str(path)}
        if offset is not None:
            args["offset"] = offset
        return self.post("Read", args, response, **event)

    def submit(self, prompt, **overrides):
        return self.run_one("UserPromptSubmit", prompt=prompt, **overrides)

    def compact(self, **overrides):
        [group] = self.hooks["SessionStart"]
        first, second = group["hooks"]
        event = {"source": "compact", **overrides}
        return tuple(self.run_hook("SessionStart", hook, **event) for hook in (first, second))

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path

    def assert_denied_for(self, output, name):
        self.assertIsNotNone(output)
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        reason = output["permissionDecisionReason"]
        for fragment in ("compact", "Skill", "呼び直し", name):
            self.assertIn(fragment, reason)
        return reason

    def test_hook_registrations(self):
        self.assertEqual(set(self.hooks), {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"})
        for name, matcher, count in (
            ("SessionStart", "compact", 2), ("UserPromptSubmit", None, 1),
            ("PreToolUse", "*", 1), ("PostToolUse", "*", 1),
        ):
            [group] = self.hooks[name]
            self.assertEqual(group.get("matcher"), matcher)
            self.assertEqual(len(group["hooks"]), count)

    def test_first_user_instructions_win_and_nonce_must_match(self):
        self.assertIsNone(self.submit(instruction_block("saved instructions")))
        text = '指示一\r\n</project-instructions nonce="abcd">\n指示二'
        self.write_transcript(calls("orchard:prune"), user([
            {"type": "text", "text": "wake"},
            {"type": "text", "text": instruction_block(f"  {text}\r\n")},
        ]), user(instruction_block("later instructions")))
        first, second = self.compact()
        self.assertEqual(first["hookEventName"], "SessionStart")
        context = first["additionalContext"]
        self.assertTrue(context.endswith(text))
        for fragment in ("compact", "プロジェクト指示の全文", "プロジェクト指示は要約の言い換え", "決まったこと", "依頼"):
            self.assertIn(fragment, context[:-len(text)])
        self.assertNotIn("saved instructions", context)
        self.assertNotIn("later instructions", context)
        self.assertIsNone(second)

    def test_prompt_instructions_overwrite_and_back_up_first_user(self):
        self.assertIsNone(self.submit(instruction_block("initial instructions")))
        self.assertIsNone(self.submit(instruction_block("  current instructions \n")))
        self.assertIsNone(self.submit('<project-instructions nonce="ab">invalid</project-instructions nonce="cd">'))
        self.write_transcript(user("no instructions"), user(instruction_block("later instructions")))
        first, second = self.compact()
        self.assertTrue(first["additionalContext"].endswith("current instructions"))
        self.assertIsNone(second)
        self.transcript.unlink()
        self.assertEqual(self.compact(), (first, second))

    def test_no_instructions_and_noncompact_session(self):
        self.write_transcript(user("no instructions"), user(instruction_block("later instructions")))
        self.assertEqual(self.compact(), (None, None))
        self.write_transcript(user(instruction_block("instructions")))
        self.assertEqual(self.compact(source="resume"), (None, None))

    def test_instructions_split_at_line_boundary_by_utf16_units(self):
        lines = [("庭🌱" * 1500) + "\r\n", "畑🍅" * 1500]
        self.write_transcript(user(instruction_block("".join(lines))))
        first, second = self.compact()
        for part, line in zip((first, second), lines):
            self.assertEqual(part["hookEventName"], "SessionStart")
            self.assertTrue(part["additionalContext"].endswith(line))
            self.assertLessEqual(len(part["additionalContext"].encode("utf-16-le")) // 2, 9000)
        self.assertNotIn(lines[1], first["additionalContext"])
        self.assertNotIn(lines[0], second["additionalContext"])
        self.assertIn("プロジェクト指示の続き", second["additionalContext"])

    def test_instructions_fallback_writes_whole_text_when_two_parts_cannot_fit(self):
        for text in ("庭\n" * 9000 + "末尾", "庭" * 9000):
            with self.subTest(lines=text.count("\n")):
                self.write_transcript(user(instruction_block(text)))
                first, second = self.compact()
                path = self.state / "session-instructions.md"
                reason = first["additionalContext"]
                self.assertIn(f"プロジェクト指示を書いたファイル {path} を Read で読み直してから続け", reason)
                self.assertNotIn("庭", reason)
                self.assertEqual(path.read_bytes().decode("utf-8"), text)
                self.assertIsNone(second)

    def test_denies_only_missing_skill_until_recalled(self):
        rows = [calls("orchard:prune", "harvest"), BOUNDARY, attached("harvest")]
        self.write_transcript(*rows)
        reason = self.assert_denied_for(self.pre(), "orchard:prune")
        self.assertNotIn("harvest", reason)
        self.write_transcript(*rows, calls("prune"))
        self.assertIsNone(self.pre())

    def test_uses_latest_compact_boundary(self):
        rows = [calls("orchard:prune"), BOUNDARY, attached("orchard:prune"),
                calls("orchard:graft", "harvest"), BOUNDARY, attached("harvest")]
        self.write_transcript(*rows)
        reason = self.assert_denied_for(self.pre(), "orchard:prune")
        self.assertIn("orchard:graft", reason)
        self.assertNotIn("harvest", reason)
        self.write_transcript(calls("orchard:prune"))
        self.assertIsNone(self.pre())

    def test_matches_qualified_skill_names_and_their_short_names(self):
        self.write_transcript(calls("garden:orchard:prune"), BOUNDARY, attached("prune"))
        self.assertIsNone(self.pre())
        self.write_transcript(calls("prune"), BOUNDARY, attached("orchard:prune"))
        self.assertIsNone(self.pre())
        self.write_transcript(calls("a:review", "b:review", "review"), BOUNDARY, attached("b:review"))
        reason = self.assert_denied_for(self.pre(), "a:review")
        self.assertNotIn("b:review", reason)

    def test_exempt_tools_pass_even_with_missing_skill(self):
        self.write_transcript(calls("orchard:prune"), BOUNDARY)
        for tool in ("Skill", "ToolSearch", POST_MESSAGE, POST_WIDGET):
            with self.subTest(tool=tool):
                self.assertIsNone(self.pre(tool))
        self.assert_denied_for(self.pre(), "orchard:prune")

    def test_missing_or_unreadable_transcript_uses_recorded_skill_calls(self):
        self.assertIsNone(self.post("Skill", {"skill": "orchard:prune"}, {"commandName": "prune"}))
        self.assertIsNone(self.post("Skill", {"skill": "orchard:graft"}))
        self.assertIsNone(self.pre())
        self.compact()
        self.assertIn("orchard:graft", self.assert_denied_for(self.pre(), "orchard:prune"))
        self.assertIsNone(self.pre(POST_MESSAGE))
        self.post("Skill", {"skill": "prune"})
        self.transcript.write_bytes(b"\xff")
        self.assertNotIn("prune", self.assert_denied_for(self.pre(), "orchard:graft"))
        self.post("Skill", {"skill": "graft"})
        self.assertIsNone(self.pre())
        self.compact()
        self.assertIn("orchard:graft", self.assert_denied_for(self.pre(), "orchard:prune"))

    def test_obsolete_configuration_is_ignored(self):
        config = {"instructions": str(self.home / "obsolete.md"), "skills": []}
        self.write(self.home / ".claude/claude-compact-restore.json", json.dumps(config))
        self.write(self.home / "obsolete.md", "obsolete instructions")
        self.env.update(CLAUDE_COMPACT_RESTORE_INSTRUCTIONS=config["instructions"],
                        CLAUDE_COMPACT_RESTORE_CONFIG=str(self.home / ".claude/claude-compact-restore.json"),
                        CLAUDE_COMPACT_RESTORE_SKILLS="unrelated:*")
        self.write_transcript(calls("prune"), BOUNDARY)
        self.assertEqual(self.compact(), (None, None))
        self.assert_denied_for(self.pre(), "prune")

    def test_only_eligible_whole_markdown_returns_with_current_text(self):
        installed = self.write(self.plugins / "plugin/guide.md", "previous guide")
        base = self.home / "checkout/skill"
        external = self.write(base / "references/note.md", "previous note")
        skill = self.write(base / "SKILL.md", "excluded skill body")
        outside = self.write(base.parent / "note.md", "excluded outside body")
        partial = self.write(self.plugins / "plugin/partial.md", "excluded offset body")
        truncated = self.write(self.plugins / "plugin/truncated.md", "excluded truncated body")
        link = self.plugins / "plugin/alias.md"
        link.symlink_to(outside)

        self.assertIsNone(self.post("Skill", {"skill": "a:skill"}, {
            "content": [{"type": "text", "text": f"Base directory for this skill: {base}\nInstructions"}],
        }))
        for path in (installed, external, skill, outside, link):
            self.assertIsNone(self.post_read(path))
        self.post_read(partial, {"file": {"startLine": 1, "numLines": 1, "totalLines": 1}}, offset=1)
        self.post_read(truncated, {"file": {"startLine": 1, "numLines": 1, "totalLines": 2}})
        self.write(installed, "current guide")
        self.write(external, "current note")
        self.write_transcript(calls("a:skill"))
        self.assertIsNone(self.pre())
        self.compact()
        self.write_transcript(calls("a:skill"), BOUNDARY, attached("a:skill"))

        output = self.pre()
        self.assertNotIn("permissionDecision", output)
        context = output["additionalContext"]
        for path, body in ((installed, "current guide"), (external, "current note")):
            self.assertIn(f"（1/1、{path}）", context)
            self.assertIn(body, context)
        for omitted in ("excluded", "previous", str(link)):
            self.assertNotIn(omitted, context)
        self.assertIsNone(self.pre())

    def test_skill_base_can_come_from_transcript_tool_result_or_meta(self):
        first = self.write(self.home / "first/external.md", "tool result note")
        second = self.write(self.home / "second/external.md", "meta text note")
        call = calls("first")
        call["message"]["content"][0]["id"] = "skill-call"
        self.write_transcript(call, user([{
            "type": "tool_result", "tool_use_id": "skill-call",
            "content": f"Base directory for this skill: {first.parent}\nInstructions",
        }]), user(f"Base directory for this skill: {second.parent}\nInstructions", isMeta=True))
        self.post_read(first)
        self.post_read(second)
        self.compact()
        self.transcript.unlink()
        context = self.pre()["additionalContext"]
        self.assertIn("tool result note", context)
        self.assertIn("meta text note", context)

    def test_only_full_file_attachments_after_latest_boundary_and_rereads_skip_injection(self):
        full = self.write(self.plugins / "full.md", "attached text")
        reference = self.write(self.plugins / "reference.md", "referenced text")
        earlier = self.write(self.plugins / "earlier.md", "previous boundary text")
        reread = self.write(self.plugins / "reread.md", "reread text")
        for path in (full, reference, earlier, reread):
            self.post_read(path)
        self.compact()
        self.write_transcript(BOUNDARY, attachment("file", filename=str(earlier)), BOUNDARY,
                              attachment("file", filename=str(full)),
                              attachment("compact_file_reference", filename=str(reference)))
        self.post_read(reread)
        context = self.pre()["additionalContext"]
        self.assertIn("referenced text", context)
        self.assertIn("previous boundary text", context)
        self.assertNotIn("attached text", context)
        self.assertNotIn("reread text", context)
        self.assertIsNone(self.pre())

    def test_bash_counts_full_output_and_named_duplicates(self):
        original = "whole Markdown line from Bash\n" * 14
        direct = self.write(self.plugins / "direct file.md", "already changed on disk")
        indirect = self.write(self.plugins / "indirect.md", original)
        self.write(self.plugins / "twin.md", original)
        self.post("Bash", {"command": f"cat -- {shlex.quote(str(direct))}"}, {"stdout": original})
        self.post("Bash", {"command": "printf output"}, {"stdout": original})
        self.post("Bash", {"command": f'for f in {shlex.quote(str(indirect))}; do cat "$f"; done'},
                  {"stdout": "before\n" + original.rstrip("\n") + "\nafter\n"})
        self.compact()
        self.write(indirect, "latest indirect Markdown")
        context = self.pre()["additionalContext"]
        self.assertIn("already changed on disk", context)
        self.assertIn("latest indirect Markdown", context)
        self.assertNotIn("twin.md", context)
        self.assertIsNone(self.pre())

    def test_bash_ignores_partial_and_truncated_output(self):
        original = ("a long Markdown line " * 4 + "\n") * 10
        path = self.write(self.plugins / "guide.md", original)
        command = f"cat {shlex.quote(str(path))}"
        self.post("Bash", {"command": command}, {"stdout": original, "stdoutTruncated": True})
        self.post("Bash", {"command": f"head -5 {shlex.quote(str(path))}"},
                  {"stdout": "".join(original.splitlines(keepends=True)[:5])})
        self.post("Bash", {"command": "printf output"}, {"stdout": "prefix " + original})
        self.compact()
        self.assertIsNone(self.pre())

    def test_long_markdown_is_delivered_across_calls_without_stopping_visible_replies(self):
        text = "📝" * 10000 + "\nlast line"
        path = self.write(self.plugins / "long.md", text)
        self.post_read(path)
        self.compact()
        pieces = []
        for index, tool in enumerate((REPLY, POST_MESSAGE, POST_WIDGET), 1):
            output = self.pre(tool)
            self.assertEqual(output["hookEventName"], "PreToolUse")
            if tool == REPLY:
                self.assertEqual(output["permissionDecision"], "deny")
                self.assertIn("もう一度呼んでください", output["permissionDecisionReason"])
            else:
                self.assertNotIn("permissionDecision", output)
            context = output["additionalContext"]
            self.assertLessEqual(len(context.encode("utf-16-le")) // 2, 9000)
            self.assertIn(f"（{index}/3、{path}）", context)
            pieces.append(context.split("\n\n", 1)[1])
        self.assertEqual("".join(pieces), text)
        self.assertIsNone(self.pre(REPLY))

    def test_unreadable_markdown_does_not_stop_reply(self):
        path = self.write(self.plugins / "deleted.md", "old Markdown")
        self.post_read(path)
        self.compact()
        path.unlink()
        self.assertIsNone(self.pre(REPLY))

    def test_markdown_changed_during_injection_restarts_with_current_text(self):
        path = self.write(self.plugins / "changed.md", "前" * 10000)
        self.post_read(path)
        self.compact()
        self.assertIn(f"（1/2、{path}）", self.pre()["additionalContext"])
        current = "今" * 10000
        self.write(path, current)
        pieces = []
        for index in (1, 2):
            context = self.pre()["additionalContext"]
            self.assertIn(f"（{index}/2、{path}）", context)
            pieces.append(context.split("\n\n", 1)[1])
        self.assertEqual("".join(pieces), current)
        self.assertIsNone(self.pre())

    def test_unreadable_state_does_not_stop_tools_without_a_transcript(self):
        self.write(self.state / "session.json", "{")
        self.assertEqual(self.compact(), (None, None))
        self.assertIsNone(self.pre(REPLY))

    def test_subagent_hooks_neither_record_nor_restore(self):
        self.write_transcript(user(instruction_block("instructions")), calls("prune"), BOUNDARY)
        path = self.write(self.plugins / "subagent.md", "subagent note")
        self.assertEqual(self.compact(agent_id="a"), (None, None))
        self.assertIsNone(self.submit(instruction_block("subagent instructions"), agent_id="a"))
        self.assertIsNone(self.post_read(path, agent_id="a"))
        self.assertIsNone(self.post("Skill", {"skill": "worker"}, agent_id="a"))
        self.assertIsNone(self.pre(agent_id="a"))
        self.transcript.unlink()
        self.assertEqual(self.compact(), (None, None))
        self.assertIsNone(self.pre())


if __name__ == "__main__":
    unittest.main()

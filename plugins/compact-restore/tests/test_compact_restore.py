from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLUGIN_ROOT.parent.parent
HOOKS_JSON_PATH = PLUGIN_ROOT / "hooks" / "hooks.json"


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def make_assistant_tool_use(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str = "toolu_1",
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": tool_input,
                }
            ]
        },
    }


def make_compact_boundary() -> dict[str, Any]:
    return {"type": "system", "subtype": "compact_boundary"}


def make_skill_meta(base_dir: str, body: str) -> dict[str, Any]:
    return {
        "type": "user",
        "isMeta": True,
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": f"Base directory for this skill: {base_dir}\n\n{body}",
                }
            ]
        },
    }


def make_user_text(content: Any, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "user",
        "message": {"content": content},
    }
    record.update(extra)
    return record


class CompactRestoreTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.config_dir = self.tmp / "config.d"
        self.config_dir.mkdir()
        self.data_dir = self.tmp / "plugin-data"
        self.data_dir.mkdir()
        self.hooks_spec = json.loads(HOOKS_JSON_PATH.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def write_config(self, filename: str, payload: Any) -> Path:
        path = self.config_dir / filename
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def write_transcript(self, path: Path, records: list[Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for rec in records:
            if isinstance(rec, str):
                lines.append(rec)
            else:
                lines.append(json.dumps(rec, ensure_ascii=False))
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return path

    def append_transcript(self, path: Path, records: list[Any]) -> None:
        with path.open("a", encoding="utf-8") as fh:
            for rec in records:
                if isinstance(rec, str):
                    fh.write(rec + "\n")
                else:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _command_for(self, event: str, index: int = 0) -> list[str]:
        entries = self.hooks_spec["hooks"][event]
        hooks = entries[0]["hooks"]
        raw_cmd = hooks[index]["command"]
        resolved = raw_cmd.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT))
        return shlex.split(resolved)

    def run_hook(
        self,
        event: str,
        payload: Any,
        *,
        hook_index: int = 0,
        config_dir: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = self._command_for(event, hook_index)
        env = os.environ.copy()
        env["COMPACT_RESTORE_CONFIG_DIR"] = str(
            config_dir if config_dir is not None else self.config_dir
        )
        env["CLAUDE_PLUGIN_DATA"] = str(self.data_dir)
        stdin_text = (
            payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        )
        return subprocess.run(
            cmd,
            input=stdin_text,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def run_session_start_all(self, payload: dict[str, Any]) -> list[str]:
        hooks = self.hooks_spec["hooks"]["SessionStart"][0]["hooks"]
        outputs: list[str] = []
        for idx in range(len(hooks)):
            res = self.run_hook("SessionStart", payload, hook_index=idx)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            if res.stdout:
                parsed = json.loads(res.stdout)
                ctx = parsed["hookSpecificOutput"]["additionalContext"]
                self.assertEqual(
                    parsed["hookSpecificOutput"]["hookEventName"], "SessionStart"
                )
                self.assertLessEqual(utf16_len(ctx), 9500)
                outputs.append(ctx)
            else:
                outputs.append("")
        return outputs


class TestCompactRestoreHooks(CompactRestoreTestBase):
    def test_no_compact_boundary_blocks_nothing(self) -> None:
        reread_dir = self.tmp / "plugins"
        doc_path = reread_dir / "guide.md"
        doc_path.parent.mkdir(parents=True)
        doc_path.write_text("# guide\n", encoding="utf-8")

        big_file = self.tmp / "big.md"
        big_file.write_text("X" * 40_000, encoding="utf-8")

        self.write_config(
            "10-main.json",
            {
                "files": [str(big_file)],
                "reread_dirs": [str(reread_dir)],
                "reinvoke_skills": ["project-rules:*"],
            },
        )
        transcript = self.write_transcript(
            self.tmp / "sess.jsonl",
            [
                make_assistant_tool_use("Read", {"file_path": str(doc_path)}),
                make_assistant_tool_use("Skill", {"skill": "project-rules:thread-rules"}),
            ],
        )

        res = self.run_hook(
            "PreToolUse",
            {
                "session_id": "s1",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "tool_use_id": "tu_1",
            },
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")

        # SessionStart context slot also does not block or emit B/C requirements
        res_ctx = self.run_hook(
            "SessionStart",
            {
                "session_id": "s1",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            },
            hook_index=4,
        )
        self.assertEqual(res_ctx.returncode, 0)
        ctx = json.loads(res_ctx.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("project-rules:thread-rules", ctx)

    def test_files_under_10000_single_slot_and_missing_ignored(self) -> None:
        inst = self.tmp / "instructions.md"
        body = "常設ルール本文\n" + ("A" * 5000)
        inst.write_text(body, encoding="utf-8")

        self.write_config(
            "10-files.json",
            {"files": [str(inst), str(self.tmp / "does-not-exist.md")]},
        )
        transcript = self.write_transcript(
            self.tmp / "sess.jsonl",
            [make_compact_boundary()],
        )

        outputs = self.run_session_start_all(
            {
                "session_id": "s-files-1",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            }
        )
        self.assertIn(body, outputs[0])
        self.assertEqual(outputs[1], "")
        self.assertEqual(outputs[2], "")

    def test_files_25000_chars_splits_across_three_slots(self) -> None:
        inst = self.tmp / "medium.md"
        lines = [f"LINE-{i:04d}-" + ("あ" * 40) for i in range(500)]
        body = "\n".join(lines)
        self.assertGreater(len(body), 24_000)
        self.assertLess(len(body), 26_500)
        inst.write_text(body, encoding="utf-8")

        self.write_config("10-files.json", {"files": [str(inst)]})
        transcript = self.write_transcript(
            self.tmp / "sess.jsonl",
            [make_compact_boundary()],
        )

        outputs = self.run_session_start_all(
            {
                "session_id": "s-files-3",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            }
        )
        self.assertTrue(outputs[0])
        self.assertTrue(outputs[1])
        self.assertTrue(outputs[2])
        self.assertIn("1/3", outputs[0])
        self.assertIn("2/3", outputs[1])
        self.assertIn("3/3", outputs[2])

        # Strip each slot header and verify the entire 25,000-char file content is intact
        stripped_parts = [slot.split("\n\n", 1)[1] for slot in outputs[:3]]
        reconstructed = "".join(stripped_parts)
        self.assertIn(body, reconstructed)

        # Does not block Bash since all files fit
        res = self.run_hook(
            "PreToolUse",
            {
                "session_id": "s-files-3",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_use_id": "tu_1",
            },
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")

    def test_files_40000_chars_overflows_and_blocks_until_read(self) -> None:
        big_file = self.tmp / "huge-rules.md"
        big_file.write_text("Z" * 40_000, encoding="utf-8")
        self.write_config("10-files.json", {"files": [str(big_file)]})
        transcript = self.write_transcript(
            self.tmp / "sess.jsonl",
            [make_compact_boundary()],
        )

        outputs = self.run_session_start_all(
            {
                "session_id": "s-files-big",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            }
        )
        self.assertIn(str(big_file), outputs[0])
        self.assertIn("Read", outputs[0])
        self.assertEqual(outputs[1], "")
        self.assertEqual(outputs[2], "")

        # PreToolUse denies Bash before Read
        deny_res = self.run_hook(
            "PreToolUse",
            {
                "session_id": "s-files-big",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "tool_use_id": "tu_1",
            },
        )
        self.assertEqual(deny_res.returncode, 0)
        parsed = json.loads(deny_res.stdout)
        self.assertEqual(parsed["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(str(big_file), parsed["hookSpecificOutput"]["permissionDecisionReason"])

        # After reading the file post-compact, Bash is allowed
        self.append_transcript(
            transcript,
            [make_assistant_tool_use("Read", {"file_path": str(big_file)})],
        )
        allow_res = self.run_hook(
            "PreToolUse",
            {
                "session_id": "s-files-big",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "tool_use_id": "tu_2",
            },
        )
        self.assertEqual(allow_res.returncode, 0)
        self.assertEqual(allow_res.stdout, "")

    def test_reread_dirs_read_bash_filtering_and_path_rewrites(self) -> None:
        plugins_dir = self.tmp / "plugins"
        outside_dir = self.tmp / "outside"
        md_read = plugins_dir / "alpha" / "doc.md"
        md_bash = plugins_dir / "beta" / "notes.md"
        txt_file = plugins_dir / "alpha" / "plain.txt"
        outside_md = outside_dir / "other.md"
        rewritten_skill_old = plugins_dir / "plugin~g2" / "skills" / "x" / "SKILL.md"
        rewritten_skill_new = plugins_dir / "plugin" / "skills" / "x" / "SKILL.md"
        missing_md = plugins_dir / "gone" / "deleted.md"

        for p in [md_read, md_bash, txt_file, outside_md, rewritten_skill_new]:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# content\n", encoding="utf-8")

        self.write_config(
            "10-reread.json",
            {
                "reread_dirs": [str(plugins_dir)],
                "path_rewrites": [["~g\\d+(?=/)", ""]],
            },
        )
        transcript = self.write_transcript(
            self.tmp / "sess.jsonl",
            [
                make_assistant_tool_use("Read", {"file_path": str(md_read)}),
                make_assistant_tool_use("Bash", {"command": f"cat {md_bash}"}),
                make_assistant_tool_use("Read", {"file_path": str(txt_file)}),
                make_assistant_tool_use("Read", {"file_path": str(outside_md)}),
                make_assistant_tool_use("Read", {"file_path": str(missing_md)}),
                make_assistant_tool_use("Read", {"file_path": str(rewritten_skill_old)}),
                make_compact_boundary(),
                # Bash cat after compact must NOT count as re-read
                make_assistant_tool_use("Bash", {"command": f"cat {md_read}"}),
            ],
        )

        deny_res = self.run_hook(
            "PreToolUse",
            {
                "session_id": "s-b",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "git status"},
                "tool_use_id": "tu_1",
            },
        )
        self.assertEqual(deny_res.returncode, 0)
        parsed = json.loads(deny_res.stdout)
        self.assertEqual(parsed["hookSpecificOutput"]["permissionDecision"], "deny")
        reason = parsed["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(str(md_read), reason)
        self.assertIn(str(md_bash), reason)
        self.assertIn(str(rewritten_skill_new), reason)
        self.assertNotIn(str(txt_file), reason)
        self.assertNotIn(str(outside_md), reason)
        self.assertNotIn(str(missing_md), reason)

        # Read all required files (using rewritten path for plugin/skills/x/SKILL.md)
        self.append_transcript(
            transcript,
            [
                make_assistant_tool_use("Read", {"file_path": str(md_read)}),
                make_assistant_tool_use("Read", {"file_path": str(md_bash)}),
                make_assistant_tool_use("Read", {"file_path": str(rewritten_skill_new)}),
            ],
        )
        allow_res = self.run_hook(
            "PreToolUse",
            {
                "session_id": "s-b",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "git status"},
                "tool_use_id": "tu_2",
            },
        )
        self.assertEqual(allow_res.returncode, 0)
        self.assertEqual(allow_res.stdout, "")

    def test_reinvoke_skills_matching_and_allowed_tools(self) -> None:
        self.write_config(
            "10-skills.json",
            {"reinvoke_skills": ["project-rules:*"]},
        )
        transcript = self.write_transcript(
            self.tmp / "sess.jsonl",
            [
                make_assistant_tool_use("Skill", {"skill": "project-rules:thread-rules"}),
                make_skill_meta(
                    "/home/u1/.claude/plugins/project-rules/skills/thread-rules",
                    "thread rules body",
                ),
                make_assistant_tool_use("Skill", {"skill": "use-pstack"}),
                make_skill_meta(
                    "/home/u1/.claude/plugins/project-rules~g3/skills/use-pstack",
                    "pstack body",
                ),
                make_assistant_tool_use("Skill", {"skill": "figure-docs:figure-docs"}),
                make_skill_meta(
                    "/home/u1/.claude/plugins/figure-docs/skills/figure-docs",
                    "figure docs body",
                ),
                make_compact_boundary(),
            ],
        )

        # Bash is denied with both project-rules skills, not figure-docs
        deny_res = self.run_hook(
            "PreToolUse",
            {
                "session_id": "s-c",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_use_id": "tu_1",
            },
        )
        parsed = json.loads(deny_res.stdout)
        self.assertEqual(parsed["hookSpecificOutput"]["permissionDecision"], "deny")
        reason = parsed["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("project-rules:thread-rules", reason)
        self.assertIn("project-rules:use-pstack", reason)
        self.assertNotIn("figure-docs", reason)

        # Read, Skill, ToolSearch, Glob, Grep are allowed even while pending
        for allowed_tool in ("Skill", "Read", "ToolSearch", "Glob", "Grep"):
            res = self.run_hook(
                "PreToolUse",
                {
                    "session_id": "s-c",
                    "transcript_path": str(transcript),
                    "cwd": str(self.tmp),
                    "hook_event_name": "PreToolUse",
                    "tool_name": allowed_tool,
                    "tool_input": {},
                    "tool_use_id": f"tu_{allowed_tool}",
                },
            )
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout, "", msg=f"{allowed_tool} should be allowed")

        # Re-invoking with and without plugin prefix satisfies both
        self.append_transcript(
            transcript,
            [
                make_assistant_tool_use("Skill", {"skill": "project-rules:thread-rules"}),
                make_assistant_tool_use("Skill", {"skill": "use-pstack"}),
            ],
        )
        allow_res = self.run_hook(
            "PreToolUse",
            {
                "session_id": "s-c",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_use_id": "tu_after",
            },
        )
        self.assertEqual(allow_res.returncode, 0)
        self.assertEqual(allow_res.stdout, "")

    def test_two_compacts_requires_all_targets_after_last_boundary(self) -> None:
        reread_dir = self.tmp / "rules"
        first_md = reread_dir / "first.md"
        second_md = reread_dir / "second.md"
        reread_dir.mkdir()
        first_md.write_text("# first\n", encoding="utf-8")
        second_md.write_text("# second\n", encoding="utf-8")

        self.write_config(
            "10-multi.json",
            {
                "reread_dirs": [str(reread_dir)],
                "reinvoke_skills": ["project-rules:*"],
            },
        )
        transcript = self.write_transcript(
            self.tmp / "sess.jsonl",
            [
                # Before first compact
                make_assistant_tool_use("Read", {"file_path": str(first_md)}),
                make_assistant_tool_use("Skill", {"skill": "project-rules:first"}),
                make_compact_boundary(),
                # Between first and second compact (re-read first, read second)
                make_assistant_tool_use("Read", {"file_path": str(first_md)}),
                make_assistant_tool_use("Read", {"file_path": str(second_md)}),
                make_assistant_tool_use("Skill", {"skill": "project-rules:first"}),
                make_assistant_tool_use("Skill", {"skill": "project-rules:second"}),
                make_compact_boundary(),
            ],
        )

        # After second compact, ALL targets from before both boundaries are required again
        deny_res = self.run_hook(
            "PreToolUse",
            {
                "session_id": "s-2c",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "echo test"},
                "tool_use_id": "tu_1",
            },
        )
        reason = json.loads(deny_res.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(str(first_md), reason)
        self.assertIn(str(second_md), reason)
        self.assertIn("project-rules:first", reason)
        self.assertIn("project-rules:second", reason)

        # Satisfy all after second boundary
        self.append_transcript(
            transcript,
            [
                make_assistant_tool_use("Read", {"file_path": str(first_md)}),
                make_assistant_tool_use("Read", {"file_path": str(second_md)}),
                make_assistant_tool_use("Skill", {"skill": "project-rules:first"}),
                make_assistant_tool_use("Skill", {"skill": "project-rules:second"}),
            ],
        )
        allow_res = self.run_hook(
            "PreToolUse",
            {
                "session_id": "s-2c",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "echo test"},
                "tool_use_id": "tu_2",
            },
        )
        self.assertEqual(allow_res.stdout, "")

    def test_truncated_and_dropped_skills_in_context(self) -> None:
        self.write_config(
            "10-d.json",
            {"reinvoke_skills": ["forced:*"]},
        )
        records: list[dict[str, Any]] = []
        # Oldest skill: 18,000 chars, will be pushed past 100,000 total by 6 newer skills
        records.append(make_assistant_tool_use("Skill", {"skill": "old:dropped-skill"}))
        records.append(
            make_skill_meta("/plugins/old/skills/dropped-skill", "D" * 18_000)
        )
        # 5 skills x 18,000 = 90,000 chars (plus 20,000 from truncated skill below = 110,000)
        for idx in range(5):
            records.append(make_assistant_tool_use("Skill", {"skill": f"mid:keep-{idx}"}))
            records.append(
                make_skill_meta(f"/plugins/mid/skills/keep-{idx}", "K" * 18_000)
            )
        # Truncated skill (> 20,000 chars, not in C)
        records.append(make_assistant_tool_use("Skill", {"skill": "big:long-skill"}))
        records.append(
            make_skill_meta("/plugins/big/skills/long-skill", "L" * 21_000)
        )
        # Truncated skill that IS in C (must NOT appear in D)
        records.append(make_assistant_tool_use("Skill", {"skill": "forced:skip-me"}))
        records.append(
            make_skill_meta("/plugins/forced/skills/skip-me", "F" * 21_000)
        )
        records.append(make_compact_boundary())

        transcript = self.write_transcript(self.tmp / "sess.jsonl", records)
        res = self.run_hook(
            "SessionStart",
            {
                "session_id": "s-d",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            },
            hook_index=4,
        )
        self.assertEqual(res.returncode, 0)
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("big:long-skill", ctx)
        self.assertIn("/plugins/big/skills/long-skill/SKILL.md", ctx)
        self.assertIn("old:dropped-skill", ctx)
        self.assertIn("/plugins/old/skills/dropped-skill/SKILL.md", ctx)
        self.assertNotIn("forced:skip-me", ctx)

    def test_other_read_files_newest_30_excludes_b(self) -> None:
        reread_dir = self.tmp / "reread"
        b_file = reread_dir / "keep.md"
        b_file.parent.mkdir()
        b_file.write_text("# keep\n", encoding="utf-8")

        self.write_config("10-e.json", {"reread_dirs": [str(reread_dir)]})
        records: list[dict[str, Any]] = [
            make_assistant_tool_use("Read", {"file_path": str(b_file)})
        ]
        for idx in range(35):
            records.append(
                make_assistant_tool_use("Read", {"file_path": f"/repo/file-{idx:02d}.py"})
            )
        records.append(make_compact_boundary())
        transcript = self.write_transcript(self.tmp / "sess.jsonl", records)

        res = self.run_hook(
            "SessionStart",
            {
                "session_id": "s-e",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            },
            hook_index=4,
        )
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn(str(b_file), ctx)
        self.assertIn("/repo/file-34.py", ctx)
        self.assertIn("/repo/file-05.py", ctx)
        self.assertNotIn("/repo/file-04.py", ctx)
        self.assertLess(ctx.index("/repo/file-34.py"), ctx.index("/repo/file-05.py"))

    def test_user_messages_ordering_filtering_stripping_and_full_file(self) -> None:
        self.write_config(
            "10-f.json",
            {
                "user_messages": {
                    "enabled": True,
                    "max_chars_each": 2000,
                    "strip": ["<session-context[\\s\\S]*?</session-context[^>]*>"],
                }
            },
        )
        long_msg = "長文の先頭:" + ("あ" * 2500) + ":長文の末尾"
        records = [
            make_user_text(
                "<system-reminder>hide default</system-reminder>\n"
                "<session-context id='1'>hide custom</session-context>\n"
                "最初の発言です"
            ),
            make_user_text("meta text", isMeta=True),
            make_user_text("summary text", isCompactSummary=True),
            make_user_text(
                [{"type": "tool_result", "tool_use_id": "t1", "content": "tool out"}]
            ),
            make_user_text("<local-command-stdout>ignored local</local-command-stdout>"),
            make_user_text("<command-name>/compact</command-name>"),
            make_user_text([{"type": "text", "text": long_msg}]),
            make_user_text("最新の発言です"),
            make_compact_boundary(),
        ]
        transcript = self.write_transcript(self.tmp / "sess.jsonl", records)

        res = self.run_hook(
            "SessionStart",
            {
                "session_id": "s-f",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            },
            hook_index=3,
        )
        self.assertEqual(res.returncode, 0)
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(utf16_len(ctx), 9500)

        # Newest first ordering
        self.assertIn("最新の発言です", ctx)
        self.assertIn("最初の発言です", ctx)
        self.assertLess(ctx.index("最新の発言です"), ctx.index("最初の発言です"))

        # Excluded lines and stripped tags do not appear
        for forbidden in (
            "meta text",
            "summary text",
            "tool out",
            "ignored local",
            "/compact",
            "hide default",
            "hide custom",
        ):
            self.assertNotIn(forbidden, ctx)

        # Long message truncated in slot with note, but full in saved file
        self.assertIn("長文の先頭:", ctx)
        self.assertNotIn(":長文の末尾", ctx)
        self.assertIn("2000文字超のため省略", ctx)

        match = re.search(r"全文の保存先:\s*(\S+)", ctx)
        self.assertIsNotNone(match)
        saved_path = Path(match.group(1))
        self.assertTrue(saved_path.is_file())
        saved_text = saved_path.read_text(encoding="utf-8")
        self.assertIn("最初の発言です", saved_text)
        self.assertIn(":長文の末尾", saved_text)
        self.assertIn("最新の発言です", saved_text)
        self.assertNotIn("hide custom", saved_text)

    def test_user_messages_slot_cap_with_many_messages_and_emoji(self) -> None:
        records = [
            make_user_text(f"発言-{i:02d}-" + ("😀" * 700))
            for i in range(12)
        ]
        records.append(make_compact_boundary())
        transcript = self.write_transcript(self.tmp / "sess-many.jsonl", records)

        res = self.run_hook(
            "SessionStart",
            {
                "session_id": "s-many",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            },
            hook_index=3,
        )
        self.assertEqual(res.returncode, 0)
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(utf16_len(ctx), 9500)
        self.assertIn("発言-11-", ctx)
        self.assertIn("件を省略", ctx)
        match = re.search(r"全文の保存先:\s*(\S+)", ctx)
        self.assertIsNotNone(match)
        saved_text = Path(match.group(1)).read_text(encoding="utf-8")
        self.assertIn("発言-00-", saved_text)
        self.assertIn("発言-11-", saved_text)

    def test_restore_tool_inputs_last_only_and_3000_cap(self) -> None:
        self.write_config(
            "10-g.json",
            {
                "restore_tool_inputs": [
                    "mcp__hearthbot__update_progress_checklist",
                    "mcp__custom__big_tool",
                ]
            },
        )
        transcript = self.write_transcript(
            self.tmp / "sess.jsonl",
            [
                make_assistant_tool_use(
                    "mcp__hearthbot__update_progress_checklist",
                    {"step": "old-step"},
                ),
                make_assistant_tool_use(
                    "mcp__hearthbot__update_progress_checklist",
                    {"step": "latest-step", "done": True},
                ),
                make_assistant_tool_use(
                    "mcp__unconfigured__tool",
                    {"secret": "should-not-appear"},
                ),
                make_assistant_tool_use(
                    "mcp__custom__big_tool",
                    {"payload": "B" * 4000},
                ),
                make_compact_boundary(),
            ],
        )

        res = self.run_hook(
            "SessionStart",
            {
                "session_id": "s-g",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            },
            hook_index=4,
        )
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(utf16_len(ctx), 9500)
        self.assertIn("mcp__hearthbot__update_progress_checklist", ctx)
        self.assertIn("latest-step", ctx)
        self.assertNotIn("old-step", ctx)
        self.assertNotIn("mcp__unconfigured__tool", ctx)
        self.assertNotIn("should-not-appear", ctx)
        self.assertIn("3000文字超のため省略", ctx)

    def test_precompact_plain_text_with_custom_instructions(self) -> None:
        self.write_config(
            "10-h.json",
            {"summary_instructions": ["チケット番号と担当者名を必ず残すこと。"]},
        )
        transcript = self.write_transcript(self.tmp / "sess.jsonl", [])
        res = self.run_hook(
            "PreCompact",
            {
                "session_id": "s-h",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreCompact",
                "trigger": "manual",
                "custom_instructions": None,
            },
        )
        self.assertEqual(res.returncode, 0)
        self.assertTrue(res.stdout.strip())
        # Must be plain text, NOT JSON
        with self.assertRaises(json.JSONDecodeError):
            json.loads(res.stdout)
        self.assertIn("会話の主な言語", res.stdout)
        self.assertIn("原文の言語", res.stdout)
        self.assertIn("決定", res.stdout)
        self.assertIn("承認された操作", res.stdout)
        self.assertIn("質問", res.stdout)
        self.assertIn("ブランチ名", res.stdout)
        self.assertIn("進捗", res.stdout)
        self.assertIn("チケット番号と担当者名を必ず残すこと。", res.stdout)
        self.assertLessEqual(utf16_len(res.stdout), 9500)

    def test_no_config_dir_still_runs_default_features(self) -> None:
        missing_cfg = self.tmp / "no-such-config-dir"
        transcript = self.write_transcript(
            self.tmp / "sess.jsonl",
            [
                make_user_text("設定なしでも戻る発言"),
                make_assistant_tool_use("Read", {"file_path": "/repo/app.py"}),
                make_compact_boundary(),
            ],
        )

        # PreCompact works
        res_pre = self.run_hook(
            "PreCompact",
            {
                "session_id": "s-nocfg",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreCompact",
                "trigger": "auto",
            },
            config_dir=missing_cfg,
        )
        self.assertEqual(res_pre.returncode, 0)
        self.assertIn("会話の主な言語", res_pre.stdout)

        # SessionStart user-messages works
        res_um = self.run_hook(
            "SessionStart",
            {
                "session_id": "s-nocfg",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            },
            hook_index=3,
            config_dir=missing_cfg,
        )
        self.assertIn(
            "設定なしでも戻る発言",
            json.loads(res_um.stdout)["hookSpecificOutput"]["additionalContext"],
        )

        # SessionStart context has I (approval scope) and E (read file)
        res_ctx = self.run_hook(
            "SessionStart",
            {
                "session_id": "s-nocfg",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            },
            hook_index=4,
            config_dir=missing_cfg,
        )
        ctx = json.loads(res_ctx.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("質問せずに続ける", ctx)
        self.assertIn("/repo/app.py", ctx)

    def test_subagent_reads_subagent_transcript_skips_a_and_g(self) -> None:
        inst = self.tmp / "parent-rules.md"
        inst.write_text("親エージェント専用ルール", encoding="utf-8")
        reread_dir = self.tmp / "sub-rules"
        sub_doc = reread_dir / "spec.md"
        sub_doc.parent.mkdir()
        sub_doc.write_text("# sub spec\n", encoding="utf-8")

        self.write_config(
            "10-sub.json",
            {
                "files": [str(inst)],
                "reread_dirs": [str(reread_dir)],
                "restore_tool_inputs": ["mcp__hearthbot__update_progress_checklist"],
            },
        )
        parent_transcript = self.write_transcript(
            self.tmp / "sess-parent.jsonl",
            [
                make_user_text("親のユーザー発言"),
                make_assistant_tool_use(
                    "mcp__hearthbot__update_progress_checklist",
                    {"step": "parent-only"},
                ),
                make_compact_boundary(),
            ],
        )
        sub_transcript = self.tmp / "sess-parent" / "subagents" / "agent-sub42.jsonl"
        self.write_transcript(
            sub_transcript,
            [
                make_user_text("サブエージェントへの依頼文"),
                make_assistant_tool_use("Read", {"file_path": str(sub_doc)}),
                make_assistant_tool_use(
                    "mcp__hearthbot__update_progress_checklist",
                    {"step": "sub-tool-should-skip"},
                ),
                make_compact_boundary(),
            ],
        )

        payload = {
            "session_id": "sess-parent",
            "transcript_path": str(parent_transcript),
            "cwd": str(self.tmp),
            "hook_event_name": "SessionStart",
            "source": "compact",
            "agent_id": "sub42",
            "agent_type": "research",
        }
        outputs = self.run_session_start_all(payload)

        # A is skipped in subagent
        self.assertEqual(outputs[0], "")
        self.assertEqual(outputs[1], "")
        self.assertEqual(outputs[2], "")

        # F comes from subagent transcript, not parent
        self.assertIn("サブエージェントへの依頼文", outputs[3])
        self.assertNotIn("親のユーザー発言", outputs[3])

        # G is skipped in subagent, I still present
        self.assertIn("質問せずに続ける", outputs[4])
        self.assertNotIn("sub-tool-should-skip", outputs[4])
        self.assertNotIn("parent-only", outputs[4])

        # B still enforced on subagent transcript
        deny_res = self.run_hook(
            "PreToolUse",
            {
                "session_id": "sess-parent",
                "transcript_path": str(parent_transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_use_id": "tu_sub",
                "agent_id": "sub42",
                "agent_type": "research",
            },
        )
        reason = json.loads(deny_res.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(str(sub_doc), reason)

    def test_fail_open_on_broken_inputs_and_configs(self) -> None:
        # 1. Broken stdin
        for event, idx in [("PreCompact", 0), ("SessionStart", 0), ("SessionStart", 3), ("SessionStart", 4), ("PreToolUse", 0)]:
            res = self.run_hook(event, "{not valid json", hook_index=idx)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout, "")

        # 2. Missing transcript_path
        missing_tr = str(self.tmp / "missing.jsonl")
        for event, idx in [("PreCompact", 0), ("SessionStart", 0), ("SessionStart", 3), ("SessionStart", 4), ("PreToolUse", 0)]:
            res = self.run_hook(
                event,
                {
                    "session_id": "s-err",
                    "transcript_path": missing_tr,
                    "cwd": str(self.tmp),
                    "hook_event_name": event,
                    "source": "compact",
                    "tool_name": "Bash",
                },
                hook_index=idx,
            )
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout, "")

        # 3. Broken config file only -> exit 0, stdout empty for PreToolUse and files 0
        self.write_config("00-broken.json", "{broken json!!!")
        transcript = self.write_transcript(
            self.tmp / "sess.jsonl",
            [
                "{broken jsonl line in middle",
                make_compact_boundary(),
            ],
        )
        res_files = self.run_hook(
            "SessionStart",
            {
                "session_id": "s-bcfg",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            },
            hook_index=0,
        )
        self.assertEqual(res_files.returncode, 0)
        self.assertEqual(res_files.stdout, "")

        res_pretool = self.run_hook(
            "PreToolUse",
            {
                "session_id": "s-bcfg",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_use_id": "tu_1",
            },
        )
        self.assertEqual(res_pretool.returncode, 0)
        self.assertEqual(res_pretool.stdout, "")

    def test_incremental_pretool_read_and_corrupt_cache_recovery(self) -> None:
        reread_dir = self.tmp / "docs"
        doc = reread_dir / "rule.md"
        reread_dir.mkdir()
        doc.write_text("# rule\n", encoding="utf-8")

        self.write_config("10-inc.json", {"reread_dirs": [str(reread_dir)]})
        transcript = self.write_transcript(
            self.tmp / "sess.jsonl",
            [
                make_assistant_tool_use("Read", {"file_path": str(doc)}),
                make_compact_boundary(),
            ],
        )
        payload = {
            "session_id": "s-inc",
            "transcript_path": str(transcript),
            "cwd": str(self.tmp),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_use_id": "tu_1",
        }

        # 1st call: denies and creates incremental cache
        res1 = self.run_hook("PreToolUse", payload)
        self.assertEqual(
            json.loads(res1.stdout)["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        cache_files = list(self.data_dir.glob("*.pretool.json"))
        self.assertEqual(len(cache_files), 1)
        offset_after_first = json.loads(cache_files[0].read_text(encoding="utf-8"))["offset"]
        self.assertEqual(offset_after_first, transcript.stat().st_size)

        # 2nd call after appending Read: reads incrementally and allows
        self.append_transcript(
            transcript,
            [make_assistant_tool_use("Read", {"file_path": str(doc)})],
        )
        res2 = self.run_hook("PreToolUse", payload)
        self.assertEqual(res2.returncode, 0)
        self.assertEqual(res2.stdout, "")
        offset_after_second = json.loads(cache_files[0].read_text(encoding="utf-8"))["offset"]
        self.assertEqual(offset_after_second, transcript.stat().st_size)
        self.assertGreater(offset_after_second, offset_after_first)

        # Corrupt cache + append another compact_boundary -> discards broken cache and re-parses from 0
        self.append_transcript(transcript, [make_compact_boundary()])
        cache_files[0].write_text("{corrupt cache", encoding="utf-8")
        res3 = self.run_hook("PreToolUse", payload)
        self.assertEqual(
            json.loads(res3.stdout)["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertIn(
            str(doc),
            json.loads(res3.stdout)["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_config_merge_order_and_plugin_manifests(self) -> None:
        self.write_config(
            "01-first.json",
            {
                "summary_instructions": ["最初の指示"],
                "user_messages": {"enabled": True, "max_chars_each": 500},
            },
        )
        self.write_config(
            "02-second.json",
            {
                "summary_instructions": ["二番目の指示"],
                "user_messages": {"enabled": False, "max_chars_each": 100},
            },
        )
        transcript = self.write_transcript(
            self.tmp / "sess.jsonl",
            [make_user_text("無効化される発言"), make_compact_boundary()],
        )

        # Lists concatenate in filename order
        res_pre = self.run_hook(
            "PreCompact",
            {
                "session_id": "s-merge",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "PreCompact",
                "trigger": "manual",
            },
        )
        self.assertIn("最初の指示", res_pre.stdout)
        self.assertIn("二番目の指示", res_pre.stdout)
        self.assertLess(res_pre.stdout.index("最初の指示"), res_pre.stdout.index("二番目の指示"))

        # Later file overrides enabled=False
        res_um = self.run_hook(
            "SessionStart",
            {
                "session_id": "s-merge",
                "transcript_path": str(transcript),
                "cwd": str(self.tmp),
                "hook_event_name": "SessionStart",
                "source": "compact",
            },
            hook_index=3,
        )
        self.assertEqual(res_um.returncode, 0)
        self.assertEqual(res_um.stdout, "")

        # Verify plugin manifests and marketplace registration
        root_manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
        claude_manifest = json.loads(
            (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        marketplace = json.loads(
            (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        self.assertEqual(root_manifest["name"], "compact-restore")
        self.assertEqual(claude_manifest["name"], "compact-restore")
        names = [p["name"] for p in marketplace["plugins"]]
        self.assertIn("compact-restore", names)


if __name__ == "__main__":
    unittest.main()

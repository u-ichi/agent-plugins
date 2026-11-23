import fcntl
import json
import os
import shlex
import sqlite3
from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class History:
    reads: list = field(default_factory=list)
    skills: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    inputs: list = field(default_factory=list)


def text_blocks(content):
    if isinstance(content, str):
        return content
    return "\n".join(
        block["text"] for block in content
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def shell_paths(command, cwd):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    words = []
    try:
        for word in lexer:
            if word and all(char in "();<>|&" for char in word):
                yield from command_paths(words, cwd)
                words.clear()
            elif word:
                words.append(word)
    except ValueError:
        pass
    yield from command_paths(words, cwd)


def command_paths(words, cwd):
    if words:
        name = Path(words[0]).name
        reader = name in {"cat", "head", "grep"} or (name == "sed" and "-n" in words[1:])
        for index, word in enumerate(words):
            yield absolute_path(word, cwd), bool(index and reader)


def absolute_path(path, cwd):
    return os.path.abspath(os.path.join(cwd, os.path.expanduser(path)))


def record(db, entry, position, cwd, tracked, pending):
    kind = entry.get("type")
    if kind == "system" and entry.get("subtype") == "compact_boundary":
        pending.clear()
        return position
    message = entry.get("message")
    if kind not in ("assistant", "user") or not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, (list, str)):
        return None
    if isinstance(content, list):
        content = [block for block in content if isinstance(block, dict)]
    if kind == "user":
        if entry.get("isCompactSummary"):
            pending.clear()
            return None
        if isinstance(content, list) and any(block.get("type") == "tool_result" for block in content):
            return None
        value = text_blocks(content)
        if entry.get("isMeta"):
            if pending:
                header, separator, body = value.partition("\n\n")
                prefix = "Base directory for this skill: "
                base = None
                if separator and header.startswith(prefix):
                    base = header[len(prefix):]
                    value = body
                db.execute(
                    "UPDATE skills SET base = ?, chars = ? WHERE rowid = ?",
                    (base, len(value), pending.popleft()),
                )
        else:
            pending.clear()
            if value and not value.startswith(("<local-command-", "<command-name>")):
                db.execute("INSERT INTO messages VALUES (?, ?)", (position, value))
        return None

    pending.clear()
    if isinstance(content, str):
        return None
    entry_cwd = entry.get("cwd")
    if isinstance(entry_cwd, str) and os.path.isabs(entry_cwd):
        cwd = entry_cwd
    for block in content:
        if block.get("type") != "tool_use":
            continue
        name, data = block.get("name"), block.get("input")
        if not isinstance(name, str) or not isinstance(data, dict):
            continue
        if name in tracked:
            db.execute("INSERT INTO inputs VALUES (?, ?, ?)", (
                position, name, json.dumps(data, ensure_ascii=False),
            ))
        if name == "Read" and isinstance(data.get("file_path"), str) and data["file_path"]:
            db.execute("INSERT INTO reads VALUES (?, ?, 1, 1)", (
                position, absolute_path(data["file_path"], cwd),
            ))
        if name == "Bash" and isinstance(data.get("command"), str):
            db.executemany(
                "INSERT INTO reads VALUES (?, ?, 0, ?)",
                ((position, path, reader) for path, reader in shell_paths(data["command"], cwd)),
            )
        if name == "Skill" and isinstance(data.get("skill"), str) and data["skill"]:
            cursor = db.execute("INSERT INTO skills VALUES (?, ?, NULL, NULL)", (position, data["skill"]))
            pending.append(cursor.lastrowid)
    return None


def index_transcript(db, stream, cwd, tracked):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS progress (state TEXT);
        CREATE TABLE IF NOT EXISTS reads (position INTEGER, path TEXT, is_read INTEGER, display INTEGER);
        CREATE TABLE IF NOT EXISTS skills (position INTEGER, name TEXT, base TEXT, chars INTEGER);
        CREATE TABLE IF NOT EXISTS messages (position INTEGER, text TEXT);
        CREATE TABLE IF NOT EXISTS inputs (position INTEGER, name TEXT, data TEXT);
        CREATE INDEX IF NOT EXISTS inputs_by_name ON inputs (name, position);
    """)
    stat = os.fstat(stream.fileno())
    identity = [stat.st_dev, stat.st_ino, sorted(tracked)]
    modified = stat.st_mtime_ns
    row = db.execute("SELECT state FROM progress").fetchone()
    if row:
        try:
            previous, size, mtime, offset, boundary, pending_ids = json.loads(row[0])
            valid = (
                previous == identity and type(size) is int and type(mtime) is int
                and type(offset) is int and 0 <= offset <= size <= stat.st_size
                and (boundary is None or type(boundary) is int and 0 <= boundary < offset)
                and isinstance(pending_ids, list) and all(type(item) is int for item in pending_ids)
            )
        except (TypeError, ValueError):
            valid = False
        if valid and size == stat.st_size and mtime == modified:
            return boundary
        if valid and size < stat.st_size:
            pending = deque(pending_ids)
        else:
            row = None
    if not row:
        for table in ("progress", "reads", "skills", "messages", "inputs"):
            db.execute("DELETE FROM " + table)
        offset, boundary, pending = 0, None, deque()

    stream.seek(offset)
    while offset < stat.st_size:
        line = stream.readline(stat.st_size - offset)
        if not line:
            break
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError):
            if not line.endswith(b"\n"):
                break
        else:
            if isinstance(entry, dict):
                marker = record(db, entry, offset, cwd, tracked, pending)
                if marker is not None:
                    boundary = marker
        offset += len(line)
    db.execute("DELETE FROM progress")
    db.execute("INSERT INTO progress VALUES (?)", (
        json.dumps([identity, stat.st_size, modified, offset, boundary, list(pending)]),
    ))
    return boundary


def load_history(transcript, cache, cwd, tracked, mode, reread, reinvoke):
    with transcript.open("rb") as stream, cache.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return read_index(cache, stream, cwd, tracked, mode, reread, reinvoke)
        except sqlite3.DatabaseError as error:
            if not any(text in str(error) for text in ("malformed", "not a database", "no such column")):
                raise
            cache.unlink()
            return read_index(cache, stream, cwd, tracked, mode, reread, reinvoke)


def read_index(cache, stream, cwd, tracked, mode, reread, reinvoke):
    with closing(sqlite3.connect(cache)) as db, db:
        boundary = index_transcript(db, stream, cwd, tracked)
        if boundary is None:
            return None
        history = History()
        if mode == "messages":
            history.messages = db.execute(
                "SELECT text FROM messages WHERE position < ? ORDER BY position", (boundary,),
            ).fetchall()
        if mode == "context":
            history.reads = db.execute(
                "SELECT path, is_read, display, 1 FROM reads WHERE position < ? ORDER BY position DESC, rowid DESC",
                (boundary,),
            ).fetchall()
            for name in sorted(tracked):
                row = db.execute(
                    "SELECT name, data FROM inputs WHERE name = ? AND position < ? "
                    "ORDER BY position DESC, rowid DESC LIMIT 1",
                    (name, boundary),
                ).fetchone()
                if row:
                    history.inputs.append(row)
        if mode == "gate" and reread:
            history.reads = db.execute(
                "SELECT DISTINCT path, is_read, display, position < ? FROM reads WHERE position < ? OR is_read",
                (boundary, boundary),
            ).fetchall()
        if mode == "context" or (mode == "gate" and reinvoke):
            history.skills = db.execute(
                "SELECT name, base, chars, position < ? FROM skills ORDER BY position DESC, rowid DESC",
                (boundary,),
            ).fetchall()
        return history

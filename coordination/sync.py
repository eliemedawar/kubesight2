#!/usr/bin/env python3
"""Message bus for the three agent tracks, over git.

The three agents share no runtime -- two Claude sessions and a ChatGPT session,
on separate accounts. The only substrate all three touch is this repository, so
that is the bus.

**One writer per file.** Each track appends only to its own mailbox and only
reads the others'. Three agents appending to one shared file is what produced
the COORDINATION.md merge conflict; with a single writer per file, concurrent
posts cannot conflict at all -- git merges three independently-appended files
without help.

    coordination/a1.md   written by A1 only, read by A2 and A3
    coordination/a2.md   written by A2 only
    coordination/a3.md   written by A3 only
    coordination/.cursors/a1.json   A1's read position, written by A1 only

Usage:

    python coordination/sync.py check --as a1
    python coordination/sync.py post  --as a1 --to a3 "guard 7 is ready"
    python coordination/sync.py post  --as a1 --to all --file note.md

`check` pulls first, prints everything addressed to you that you have not seen,
and advances your cursor. It exits 0 when there was nothing and 10 when there
was something -- so a scheduler can act only when there is actually traffic.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
CURSORS = ROOT / ".cursors"
TRACKS = ("a1", "a2", "a3")

# "## 2026-08-02T16:45:00Z to:a3"
HEADER = re.compile(r"^##\s+(?P<ts>\d{4}-\d{2}-\d{2}T[\d:]+Z)\s+to:(?P<to>[a-z0-9,]+)\s*$")

EXIT_NOTHING = 0
EXIT_MESSAGES = 10


def _git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _mailbox(track: str) -> Path:
    return ROOT / f"{track}.md"


def _cursor_path(track: str) -> Path:
    return CURSORS / f"{track}.json"


def _read_cursor(track: str) -> dict:
    path = _cursor_path(track)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt cursor should replay messages, never silently swallow them.
        return {}


def _write_cursor(track: str, cursor: dict) -> None:
    CURSORS.mkdir(parents=True, exist_ok=True)
    _cursor_path(track).write_text(
        json.dumps(cursor, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _parse(path: Path) -> list[dict]:
    """Split a mailbox into entries. Unparseable text before the first header
    is ignored, so a human can put a preamble at the top."""
    if not path.is_file():
        return []
    entries: list[dict] = []
    current: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = HEADER.match(line)
        if match:
            if current:
                entries.append(current)
            current = {
                "ts": match.group("ts"),
                "to": set(match.group("to").split(",")),
                "body": [],
            }
        elif current is not None:
            current["body"].append(line)
    if current:
        entries.append(current)
    return entries


def cmd_check(args: argparse.Namespace) -> int:
    me = args.track
    if not args.no_pull:
        # --ff-only: a merge commit made unattended by a bot is a merge nobody
        # reviewed. If it cannot fast-forward, a human should look.
        try:
            _git("pull", "--ff-only")
        except RuntimeError as exc:
            print(f"pull failed, reading local state only: {exc}", file=sys.stderr)

    cursor = _read_cursor(me)
    found = 0

    for peer in TRACKS:
        if peer == me:
            continue
        last_seen = cursor.get(peer, "")
        for entry in _parse(_mailbox(peer)):
            if entry["ts"] <= last_seen:
                continue
            if me not in entry["to"] and "all" not in entry["to"]:
                continue
            body = "\n".join(entry["body"]).strip()
            print(f"\n=== from {peer.upper()} at {entry['ts']} ===")
            print(body)
            found += 1
            cursor[peer] = max(cursor.get(peer, ""), entry["ts"])

    if not args.peek:
        _write_cursor(me, cursor)

    if found == 0:
        print("no new messages")
        return EXIT_NOTHING
    print(f"\n{found} new message(s)")
    return EXIT_MESSAGES


def cmd_post(args: argparse.Namespace) -> int:
    me = args.track
    body = args.message
    if args.file:
        body = Path(args.file).read_text(encoding="utf-8")
    if not body or not body.strip():
        print("refusing to post an empty message", file=sys.stderr)
        return 1

    recipients = ",".join(sorted(set(args.to.split(","))))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    mailbox = _mailbox(me)
    existing = mailbox.read_text(encoding="utf-8") if mailbox.is_file() else _preamble(me)
    mailbox.write_text(
        f"{existing.rstrip()}\n\n## {stamp} to:{recipients}\n{body.strip()}\n",
        encoding="utf-8",
    )

    if args.commit:
        _git("add", str(mailbox.relative_to(REPO)))
        _git("commit", "-q", "-m", f"coordination: {me} -> {recipients}")
        if args.push:
            _git("push")
    print(f"posted to {recipients} at {stamp}")
    return 0


def _preamble(track: str) -> str:
    return (
        f"# {track.upper()} outbox\n\n"
        f"Only {track.upper()} writes to this file. The other tracks read it.\n"
        "Single-writer is what keeps three agents from conflicting here.\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sync.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="show unread messages addressed to you")
    check.add_argument("--as", dest="track", choices=TRACKS, required=True)
    check.add_argument("--no-pull", action="store_true")
    check.add_argument("--peek", action="store_true", help="do not advance the cursor")
    check.set_defaults(fn=cmd_check)

    post = sub.add_parser("post", help="append a message to your own mailbox")
    post.add_argument("--as", dest="track", choices=TRACKS, required=True)
    post.add_argument("--to", default="all")
    post.add_argument("message", nargs="?", default="")
    post.add_argument("--file")
    post.add_argument("--commit", action="store_true", default=True)
    post.add_argument("--no-commit", dest="commit", action="store_false")
    post.add_argument("--push", action="store_true")
    post.set_defaults(fn=cmd_post)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

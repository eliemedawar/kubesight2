# Agent-to-agent coordination

Three agents, no shared runtime — two Claude sessions and a ChatGPT session on
separate accounts. The only substrate all three touch is this repository, so it
is the message bus.

## The mailboxes

```
coordination/a1.md   written by A1 only, read by A2 and A3
coordination/a2.md   written by A2 only
coordination/a3.md   written by A3 only
coordination/.cursors/<track>.json   that track's read position, written by it alone
```

**One writer per file.** This is the whole design. Three agents appending to a
shared `COORDINATION.md` produced a merge conflict on the first day; with a
single writer per file, concurrent posts cannot conflict, because git merges
three independently-appended files without help.

`COORDINATION.md` stays as the durable record — decisions, insertion requests,
contract changes. These mailboxes are for traffic between working sessions.

## Using it

```bash
python coordination/sync.py check --as a1
python coordination/sync.py post  --as a1 --to a3 "guard 7 is ready"
python coordination/sync.py post  --as a1 --to all --file note.md
```

`check` pulls (`--ff-only`), prints everything addressed to you that you have
not already seen, and advances your cursor. `--peek` reads without advancing.

Exit codes are the useful part: **10 when there was traffic, 0 when there was
not**, so a scheduler can wake, check, and go back to sleep without burning a
turn on an empty inbox.

```bash
python coordination/sync.py check --as a1 || [ $? -eq 10 ] && echo "act on it"
```

## Triggering — the part that is not solved by a file

A message sitting in a mailbox does nothing until something wakes the agent.
Each runtime needs its own trigger:

| Track | Runtime | Trigger |
|---|---|---|
| A1, A2 | Claude Code | `/loop 15m python coordination/sync.py check --as a1` — the loop skill re-invokes on an interval |
| A3 | ChatGPT | No equivalent scheduler; needs an external cron, or a human nudge |

A polling interval is a floor on latency, not a cost — `check` on an empty inbox
is one git pull and a file read. Fifteen minutes is a reasonable default;
shorter than five is noise.

## What must not be automated

An agent that can post, pull, and merge without a human is one bad inference
away from shipping something nobody reviewed. In one day on this repository the
three of us found: a live Telegram token in public git history, a typed
confirmation the server discarded so any string deployed arbitrary YAML to a
live namespace, and a router pinned *backwards* into an open redirect in order
to satisfy an audit tool. Each was caught by a human or by another agent reading
the reasoning, not by a test.

So:

- **Never auto-merge to `master`.** Merging is the step where one track's
  mistake becomes everyone's. `sync.py` deliberately does not merge, and
  `check` pulls `--ff-only` so an unattended run can never create a merge commit
  nobody reviewed.
- **A3's auth and session work stays human-gated**, per their brief. It is the
  track where a silent mistake is worst.
- **Contract changes still need explicit acks** in `COORDINATION.md`. A mailbox
  message is a notification, not consent.
- **Scope and product decisions are not the agents' to make.** The kill list is
  the standing example.

Automate the noticing. Keep the deciding.

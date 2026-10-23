---
name: looper-skill
description: Hand a discussed piece of work to the local Looper (a multi-agent system that plans and runs coding tasks) and keep working with it - submit through its Manager, answer its questions, start, stop and change tasks, check status, pipeline and gates, manage Agent Profiles - using only the loopercli command. Use when the user asks to give work to Looper, or asks about a Looper task.
---

# Working with Looper through loopercli

Looper runs on this machine. You reach it only with `loopercli` (never curl, never its files or database). Run `loopercli whoami` first; if it fails, tell the user what it printed (usually: start `looper serve`, or create a token in Settings → API clients and run `loopercli login`; when `looper serve` does not start, the user's `looper doctor` says why). Never put the token in a command, file or message.

## Hand over new work: talk to the Manager

1. Write one message for the Manager: the goal, the scope, what "done" means (acceptance), the project and directory if the user named them, and the paths of the documents you wrote with the user. Summarise long documents and point to their paths; a message holds at most 32,000 characters. If the user already told you in this session to submit (and start) this work, say so in the message ("the user agreed to submit and start"): the Manager may then create (and start) the task at once.
2. Send it: `loopercli chat --file <message file>` (or `loopercli chat "<text>"`). It prints `conversation: cli-...` on stderr: keep that id and pass `--conversation <id>` on every later message about this work.
3. The Manager restates the task and asks questions (project, directory, details). Answer from what you and the user settled in this session. If the discussion does not answer a question, ask the user; never invent an answer.
4. When the Manager asks for confirmation, decide as below, then reply in the same conversation (for example `loopercli chat --conversation <id> "确认"`).

## What you may confirm for the user

Only when the user said in this session to submit, start or go ahead, you may confirm on their behalf:
- creating this task and starting it (including a new project the task needs, named as you discussed);
- editing this task's requirement;
- answering this task's gates with what the user decided.

Always relay to the user first, word for word, and confirm only after they agree:
- cancelling or deleting anything;
- changing settings, Agent Profiles or providers;
- anything you are not sure about.

## Follow a task

- `loopercli status <task>` – status, steps, gates, result; `loopercli status` – everything running.
- `loopercli pipeline <task>` – the steps with their dependencies and states.
- `loopercli wait <task>` – blocks until a gate opens for the user, the task ends, or the timeout passes (run it in the background if you can); exit 6 means it timed out. This is how you follow gates and progress: they do not come into your conversation.
- `loopercli chat --conversation <id> --read` – the Manager's messages in your conversation, including the task's acceptance and its end (not its gates or progress).
- `loopercli list` – recent tasks.

## Answer a gate

`loopercli gates list --waiting-on user` lists gates waiting for an answer; `loopercli gates messages --task-id <task> --gate <g1>` shows the discussion; `loopercli gates reply --task-id <task> --gate <g1> --text "<answer>"` answers; `loopercli gates end --task-id <task> --gate <g1>` lets the Planner go on with what is settled. Answer only with what the user decided.

## Change a task

Ask the Manager in your conversation, or directly: `loopercli task-update --task-id <task> --requirement-file <file>`, `loopercli task-instruct --task-id <task> --text "<words>"` (a running task), `loopercli start <task>`, `loopercli stop <task>`. Cancelling (`loopercli task-control cancel --task-id <task>`) follows the relay rule above.

## Agent Profiles and the rest

`loopercli agent-profiles list`; `create`, `update` and `delete` follow the relay rule. `loopercli --help` lists every operation; `loopercli <command> --help` its options. Add `--json` for machine-readable output.

## Confirmations of direct commands

A direct command that needs confirmation exits 3 and prints what it would do and `loopercli confirm <id>`. Apply the rules above, then run that command (same session, within 10 minutes).

## Do not

- Do not send `/new` unless the user asks: it starts a new session for the Manager on every channel.
- Do not use curl, Looper's files, its database or another Looper command.

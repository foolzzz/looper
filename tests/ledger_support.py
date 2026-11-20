"""What the ledger tests share: synthetic tasks, gates and confirmations to point sourced items at."""

from looper.db import ledger

USER = ledger.Actor("user", "dashboard", actor_id="dashboard")
RELAYED = ledger.Actor("relayed", "api", actor_id="a colleague", relayed_by="client:demo")
SYSTEM = ledger.Actor("system", "system")
CLI = ledger.Actor("user", "cli", actor_id="cli", session_label="test session")


async def seed(pool):
    """A project, a task with a gate step, and a Manager's confirmation question; their ids."""
    await pool.execute("INSERT INTO projects (id, name) VALUES ('proj', 'Demo project')")
    await pool.execute("INSERT INTO tasks (id, requirement, project_id, title) VALUES ('task-1', 'Do it', 'proj', 'Do the thing')")
    await pool.execute("INSERT INTO task_steps (id, task_id, step_index, instruction, kind) "
                       "VALUES ('gate-1', 'task-1', 0, 'Ask', 'gate')")
    await pool.execute("INSERT INTO task_user_gates (step_id, task_id, topic, state) "
                       "VALUES ('gate-1', 'task-1', 'Pick a database', 'open')")
    action_id = await pool.fetchval(
        "INSERT INTO manager_actions (tool, risk, status, confirmation_id) "
        "VALUES ('looper_task_control', 'high', 'needs_confirmation', 'c-1') RETURNING id")
    return {"project": "proj", "task": "task-1", "gate": "gate-1", "action": action_id}


async def add_own(pool, kind="pending", actor=USER, title="Reply by Friday", **fields):
    return await ledger.apply("add", actor, new=ledger.NewItem(kind=kind, title=title, **fields), db=pool)


async def add_sourced(pool, seeded, source):
    columns = {"gate": {"kind": "pending", "source_kind": "gate", "gate_step_id": seeded["gate"]},
               "task": {"kind": "work_item", "source_kind": "task", "task_id": seeded["task"]},
               "confirmation": {"kind": "pending", "source_kind": "confirmation", "action_id": seeded["action"]}}[source]
    return await ledger.apply("add", SYSTEM, new=ledger.NewItem(title=f"from {source}", **columns), db=pool)

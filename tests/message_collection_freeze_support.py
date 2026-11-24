"""Capture individual public contracts on a pinned revision, without launching models."""

import hashlib
import json
from pathlib import Path

BASELINE = "39f329ac444f0f6d9783011b258626dc3679095d"


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def snapshot():
    from looper.agents import api_v1, processor, system_prompt
    from looper.agents.agent_settings import Access, AgentSettings, AgentType, Launch
    from looper.agents.profile_registry import Endpoint, ModelSpec, ProviderSpec, ResolvedModel, ResolvedAgentProfile, RuntimeSpec
    from looper.agents.runtimes import ApiProtocol, AuthKind, VendorType, adapter
    from looper.agents.ops import clients, planner as planner_ops, room, service
    from looper.agents.tiers import manager, planner, worker

    values = {}
    roles = {
        "manager": (manager.DEFAULT_SYSTEM_PROMPT, manager.TOOLS_CONTRACT),
        "planner": (planner.DEFAULT_SYSTEM_PROMPT, planner.reply_contract()),
        "worker": (worker.DEFAULT_SYSTEM_PROMPT, system_prompt.WORK_CONTRACT),
        "reviewer": (worker.REVIEWER_SYSTEM_PROMPT, system_prompt.REVIEW_CONTRACT),
        "processor": (processor.PROCESSOR_ROLE_PROMPT, processor.reply_contract("SKIP")),
    }
    provider = ProviderSpec("provider-synthetic", "Synthetic", VendorType.CUSTOM, AuthKind.API_KEY,
                            api_key="synthetic-key")
    model = ModelSpec("model-synthetic", provider.id, "synthetic-model", ApiProtocol.ANTHROPIC_MESSAGES,
                     input_usd_per_mtok=1, output_usd_per_mtok=2)
    binding = ResolvedModel(model, provider, Endpoint(provider.id, model.protocol, "https://example.invalid"))
    profile = ResolvedAgentProfile("profile-synthetic", "Synthetic", RuntimeSpec("runtime-synthetic", "Synthetic",
        "claude-code", capabilities=adapter("claude-code").capabilities), model.id, resolved_model=binding)
    settings = AgentSettings(profile=profile)
    for role, (prompt, contract) in roles.items():
        if role == "processor":
            final = processor._system_prompt(settings, processor._Role(prompt, True, 600, 0, True),
                                               "Summarize synthetic messages.", "SKIP").text
        else:
            final = system_prompt.build_system_prompt(harness=settings.harness, role_id=role, role=prompt,
                recommended=True, contract_name=role.upper() + "_CONTRACT", contract=contract).text
        launch = Launch(settings, role=AgentType(role), access=Access.ADVISOR if role == "processor" else None)
        values[f"prompt/{role}"] = final
        values[f"contract/{role}"] = contract
        argv = launch.runtime.launch_args(launch, cwd=Path("/synthetic-work"))
        values[f"argv/{role}"] = argv
        values[f"process/{role}"] = [argv, hashlib.sha256(final.encode()).hexdigest(),
            launch.env_sha256, launch.library.sha256, launch.binding_sha256, launch.system_prompt_mode.value]
    for audience, tools in (("manager", service.MANAGER_TOOLS), ("client", clients.CLIENT_TOOLS),
                             ("planner", (*planner_ops.TOOLS, *planner_ops.ROLE_TOOLS)), ("room", room.TOOLS)):
        for tool in tools:
            values[f"tool/{audience}/{tool.name}"] = {"name": tool.name, "description": tool.description,
                "inputSchema": tool.schema, "risk": tool.risk.value, "read_actions": sorted(tool.read_actions)}
    for operation in api_v1.operations():
        if operation.operation_id.startswith("message_stream."):
            values[f"operation/{operation.operation_id}/request"] = operation.request
            values[f"operation/{operation.operation_id}/result"] = operation.result
            values[f"operation/{operation.operation_id}/description"] = operation.description
    from looper.agents.channels import lark_cli
    values["channel/lark/post"] = lark_cli.send_command(user_id="ou_synthetic", text="Synthetic update.", profile="synthetic")
    values["channel/lark/reply"] = lark_cli.reply_command("om_synthetic", text="Synthetic reply.", profile="synthetic")
    return {name: hashlib.sha256(encoded(value).encode()).hexdigest() for name, value in sorted(values.items())}


async def database_snapshot(pool, root, mode):
    from looper.agents import attention, msgflow_commands, source_service
    from looper.agents.v1_domains import message_stream
    from looper.db import connection, message_stream as store
    from tests import msgflow_support as flow

    connection._pool = pool
    values = {}
    if mode != "no_source":
        flow.append(root, "oc_synthetic", flow.message("om_synthetic", text="Synthetic baseline."))
        flow.write_names(root, chats={"oc_synthetic": {"name": "Synthetic group", "type": "group"}})
        source = await store.save_source("synthetic", {"ingest_mode": mode, "bridge_dir": str(root),
            "bridge_start_from": "beginning", "cron": source_service.BRIDGE_CRON, "user_open_id": flow.ME}, db=pool)
        values["configure"] = await message_stream._source_view(source)
        if mode == "bridge":
            original_clock = source_service.datetime
            class Clock(original_clock):
                @classmethod
                def now(cls, tz=None):
                    return flow.NOW.astimezone(tz)
            source_service.datetime = Clock
            try:
                await source_service.run_bridge_pass(source, db=pool, now=flow.NOW)
            finally:
                source_service.datetime = original_clock
        report = await source_service.run_bridge_pass(source, db=pool, now=flow.NOW, days=7, dry_run=True)
        values["import/text"] = msgflow_commands.format_report(report, days=7, dry_run=True)
        values["import/per_file"] = msgflow_commands.format_report(report, days=7, dry_run=True, per_file=True)
        values["import/exit"] = 0 if report.status in ("ok", "partial") else 1
    values["status"] = {"sources": [await message_stream._source_view(s) for s in await store.list_sources(db=pool)]}
    values["runs"] = await store.list_runs("synthetic", limit=20, db=pool)
    schedule, problem = await msgflow_commands._leads_schedule(pool, None)
    assert schedule is None
    values["leads-backfill/no-schedule"] = [1, problem]
    for table in ("event_sources", "source_chats", "source_threads", "source_people", "source_events", "source_runs",
                  "subscriptions", "subscription_runs", "message_leads", "message_lead_sources", "intents"):
        values["rows/" + table] = [dict(row) for row in await pool.fetch(f"SELECT * FROM {table} ORDER BY 1,2")]
    old = attention._catchup_checks
    try:
        attention._catchup_checks = None
        values["attention/empty"] = await attention._deliveries(None)
        attention._catchup_checks = lambda: [{"id": "lark_catchup:synthetic", "detail": "Synthetic catchup debt",
            "label": "Synthetic channel", "status": "fail", "fix": "Inspect the local channel"}]
        values["attention/catchup"] = await attention._deliveries(None)
    finally:
        attention._catchup_checks = old
    if mode == "bridge":
        from looper.agents.triage_why import why, format_lines
        answer = await why(pool, "om_synthetic", now=flow.NOW)
        values["why/json"] = answer.as_dict()
        values["why/text"] = "\n".join(format_lines(answer))

    def normalize(value, key=None):
        if value is not None and key in ("created_at", "updated_at", "ingested_at", "bridge_inode", "bridge_mtime"):
            return "<" + key + ">"
        if isinstance(value, dict):
            return {name: normalize(item, name) for name, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            return value.replace(str(root), "<fixture-root>")
        return value

    return {name: hashlib.sha256(encoded(normalize(value)).encode()).hexdigest() for name, value in values.items()}


if __name__ == "__main__":
    import asyncio
    import sys
    import tempfile

    result = {"baseline_revision": BASELINE, "items": snapshot()}
    if "--database" in sys.argv:
        from tests.db_support import fresh_schema
        from looper.db.connection import ensure_schema

        async def capture():
            rows = {}
            for mode in ("no_source", "off", "bridge"):
                async with fresh_schema() as pool:
                    await ensure_schema(pool)
                    with tempfile.TemporaryDirectory(prefix="collection-fixture-") as tmp:
                        rows[mode] = await database_snapshot(pool, Path(tmp), mode)
            return rows
        result["database_items"] = asyncio.run(capture())
    print(encoded(result))

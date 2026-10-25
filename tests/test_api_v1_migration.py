"""All retired operator routes stay absent at dispatch, with declared public replacements.

The matching v1 success controls and state readback belong to the 33 domain test
files; this shared guard uses whoami only to prove authentication on the same server.
The inventory comes from the former HTTP dispatch, including its streamed ingress.
"""

import asyncio

import pytest

from looper.agents import api_v1, operator_auth
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

# Each entry is METHOD, the old path below /api/, and the replacement operationId.
LEGACY_ROUTES = [tuple(row.split()) for row in """
GET status system.status; GET overview system.overview; GET tasks tasks.page; POST chat/send operator_chat.send; GET manager/session manager_session.get; PUT manager/session/profile manager_session.select_profile;
GET chat/targets operator_chat.targets; GET chat/sessions operator_chat.sessions; GET chat/messages operator_chat.messages; POST chat/messages operator_chat.save_message; POST chat/stop operator_chat.stop; GET chat/turn operator_chat.turn;
GET chat/events operator_chat.reply_events; POST tasks/create tasks.create; POST tasks/cancel task_control.cancel; POST tasks/stop task_control.stop; POST tasks/start task_control.restart; POST tasks/delete tasks.delete;
GET tasks/{task_id}/activity/stream tasks.activity_stream; GET tasks/{task_id}/activity/{seq} tasks.activity_detail; GET tasks/{task_id}/activity tasks.activity; GET tasks/{task_id}/conversation tasks.conversation; GET tasks/{task_id}/runs runs.for_task; PUT tasks/{task_id}/config tasks.configure;
PUT tasks/{task_id}/project tasks.move; POST tasks/{task_id}/continue tasks.continue; PUT tasks/{task_id}/requirement tasks.requirement_update; PUT tasks/{task_id}/title task_update; PUT tasks/{task_id}/agents tasks.agents; PUT tasks/{task_id}/roles/{key} tasks.role_update;
GET gates gates.list; GET tasks/{task_id}/gates/{gate}/messages gates.messages; POST tasks/{task_id}/gates/{gate}/messages gates.reply; POST tasks/{task_id}/gates/{gate}/end gates.end; POST tasks/{task_id}/waits/{step}/resume waits.resume; POST tasks/{task_id}/waits/{step}/cancel waits.cancel;
POST tasks/{task_id}/workspaces/{workspace_id}/discard workspaces.discard; POST tasks/{task_id}/start task_control.start; GET tasks/{task_id}/room tasks.room_view; GET tasks/{task_id} tasks.detail; GET workspaces/check workspaces.check; GET clients clients.list;
POST clients clients.create; DELETE clients/{client_id} clients.revoke; GET projects project_admin.list; POST projects project_admin.create; PUT projects/{project_id} project_admin.update; DELETE projects/{project_id} project_admin.delete;
GET projects/{project_id}/schedules schedule_admin.project_list; POST projects/{project_id}/schedules schedule_admin.create; GET schedules schedule_admin.list; POST schedules/preview schedule_admin.preview; GET schedules/{schedule_id} schedule_admin.get; PUT schedules/{schedule_id} schedule_admin.update;
DELETE schedules/{schedule_id} schedule_admin.delete; POST schedules/{schedule_id}/reset-planner-session schedule_admin.reset_planner_session; POST schedules/{schedule_id}/run schedule_admin.run; GET schedules/{schedule_id}/runs schedule_admin.runs; GET schedules/{schedule_id}/notifications schedule_admin.notifications; GET projects/{project_id}/knowledge project_knowledge.list;
POST projects/{project_id}/knowledge project_knowledge.add; GET projects/{project_id}/knowledge/{document_id} project_knowledge.get; PUT projects/{project_id}/knowledge/{document_id} project_knowledge.update; DELETE projects/{project_id}/knowledge/{document_id} project_knowledge.delete; GET projects/{project_id}/knowledge/drafts project_knowledge.drafts; POST projects/{project_id}/knowledge/drafts/{draft_id}/confirm project_knowledge.confirm_draft;
DELETE projects/{project_id}/knowledge/drafts/{draft_id} project_knowledge.discard_draft; GET projects/{project_id}/installs installs.project; POST projects/{project_id}/skills project_skills.add; GET projects/{project_id}/skills/{skill_id} project_skills.get; PUT projects/{project_id}/skills/{skill_id} project_skills.update; DELETE projects/{project_id}/skills/{skill_id} project_skills.delete;
GET roles roles.list; GET roles/{role} roles.get; POST roles/{role}/reset-prompt roles.reset_prompt; PUT roles/{role} roles.update; GET llm-records runs.list; GET llm-records/{record_id} runs.get;
GET llm-records/{record_id}/events runs.events; GET agents/{agent_id}/usage runs.usage; GET registry registry.view; POST runtimes/{runtime_id}/probe runtimes.probe; GET providers provider_accounts.list; POST providers provider_accounts.create;
PUT providers/{provider_id} provider_accounts.update; DELETE providers/{provider_id} provider_accounts.delete; DELETE providers/{provider_id}/login-problem provider_accounts.clear_login_problem; GET models models.list; POST models models.create; GET models/{model} models.get;
PUT models/{model} models.update; DELETE models/{model} models.delete; GET agent-profiles/{profile_id}/prompt-preview profiles.prompt_preview; POST agent-profiles profiles.create; PUT agent-profiles/{profile_id} profiles.update; DELETE agent-profiles/{profile_id} profiles.delete;
GET agent-settings agent_settings.get; PUT agent-settings/system agent_settings.update_system; PUT agent-settings/roles/{role} agent_settings.update_role; GET projects/{project_id}/agent-settings project_agent_settings.get; PUT projects/{project_id}/agent-settings project_agent_settings.update; GET projects/{project_id}/allowed-profiles allowed_profiles.get;
PUT projects/{project_id}/allowed-profiles allowed_profiles.replace; GET projects/{project_id}/role-stats project_roles.stats; GET projects/{project_id}/role-templates role_templates.list; PUT projects/{project_id}/role-templates/{key} role_templates.put; DELETE projects/{project_id}/role-templates/{key} role_templates.delete; GET managers managers.list;
POST managers managers.create; PUT managers/{manager_id} managers.update; DELETE managers/{manager_id} managers.delete; PUT managers/{manager_id}/templates/{key} manager_templates.put; DELETE managers/{manager_id}/templates/{key} manager_templates.delete; PUT projects/{project_id}/agent-settings/roles/{role} project_agent_settings.update_role;
GET config/export config.export; POST config/imports/{import_id}/apply config.import_apply; DELETE config/imports/{import_id} config.import_cancel; GET catalog catalog_items.list; POST catalog/import catalog_sources.import_machine; POST catalog/upload catalog_sources.upload;
POST catalog/git/preview catalog_sources.git_preview; POST catalog/git/import catalog_sources.git_import; POST catalog/skills catalog_sources.create_skill; POST catalog/mcp catalog_sources.create_mcp; POST catalog/mcp/import catalog_sources.import_mcp; GET catalog/{item_id} catalog_items.get;
PUT catalog/{item_id} catalog_items.update; DELETE catalog/{item_id} catalog_items.delete; POST installs installs.create; PUT installs/{install_id} installs.update; DELETE installs/{install_id} installs.delete; GET channels/lark/status channels.status;
GET channels/lark/people channels.people; GET channels/lark_bot/status channels.status; GET channels/lark_bot/people channels.people; GET scheduler-settings scheduler_settings.get; PUT scheduler-settings scheduler_settings.update; GET chat-settings chat_settings.get;
PUT chat-settings chat_settings.update; GET gate-settings gate_settings.get; PUT gate-settings gate_settings.update; GET wait-settings wait_settings.get; PUT wait-settings wait_settings.update; GET knowledge-vault knowledge_vault.get;
PUT knowledge-vault knowledge_vault.update; GET channel-configs channel_configs.list; GET channel-configs/{channel_id} channel_configs.get; POST channel-configs channel_configs.create; PUT channel-configs/{channel_id} channel_configs.update; DELETE channel-configs/{channel_id} channel_configs.delete;
POST config/imports config.import_upload;
""".split(";") if row.strip()]


def _legacy_path(path):
    values = {part.split("}", 1)[0]: "example" for part in path.split("{")[1:]}
    values.update(task_id="task-example", project_id="proj-example", schedule_id="sched-example",
                  document_id="doc-example", draft_id="draft-example", skill_id="skill-example",
                  item_id="skill-example", install_id="inst-example", manager_id="manager-example",
                  provider_id="prov-example", model="model-example", profile_id="agent-example",
                  runtime_id="rt-example", client_id="client-000000000000", import_id="cfgimp-0000000000000000",
                  role="worker", seq=1, workspace_id=1, record_id=1)
    return "/api/" + path.format(**values)


@pytest.mark.parametrize("method,path,operation_id", LEGACY_ROUTES)
def test_retired_operator_dispatch_has_authenticated_404(tmp_path, method, path, operation_id):
    path = _legacy_path(path)
    assert operator_auth.access(path) is operator_auth.Access.OPERATOR
    operation = next(op for op in api_v1.operations() if op.operation_id == operation_id)
    assert operation.path.startswith("/api/v1/") and api_v1.find(operation.method, operation.path) == operation

    async def run():
        async with _serving(tmp_path) as server:
            status, _, identity = await _send(server, "GET /api/v1/whoami", _bearer(TOKEN))
            assert status == 200 and identity["result"]["principal"] == "operator"
            status, _, answer = await _send(server, f"{method} {path}", _bearer(TOKEN), "{}")
            assert status == 404 and answer == {"error": "not found"}
    asyncio.run(run())


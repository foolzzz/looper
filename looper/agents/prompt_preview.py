"""Read-only profile prompt previews, composed with the same builders as launches."""
from __future__ import annotations

from dataclasses import asdict, replace

from ..db import profile_registry as db_profiles, projects as db_projects, role_configs as db_roles
from ..db import project_documents as db_documents
from .agent_settings import AgentType, Launch, load_agent_settings, profile_problem
from .knowledge import AgentKnowledge, SourceKind, StoredSet, _entry, clipped_index
from .profile_registry import AgentProfile, RegistryError, RegistryNotFound
from .system_prompt import WORK_CONTRACT, REVIEW_CONTRACT, build_system_prompt, is_recommended


async def _role_prompt(role: AgentType) -> tuple[str, bool]:
    if role in (AgentType.WORKER, AgentType.REVIEWER):
        from .tiers.worker import _load_settings

        settings = await _load_settings(role)
        return settings.system_prompt, settings.recommended
    if role is AgentType.PLANNER:
        from .tiers.planner import load_settings

        settings = await load_settings()
        return settings.system_prompt, settings.recommended
    from .tiers.manager import DEFAULT_SYSTEM_PROMPT

    config = await db_roles.get_config(role.value)
    text = (config or {}).get("system_prompt") or DEFAULT_SYSTEM_PROMPT
    recommended = (config or {}).get("default_prompt") or DEFAULT_SYSTEM_PROMPT
    return text, is_recommended(text, recommended)


async def preview(profile_id: str, role: str, project_id: str | None = None) -> dict:
    """Preview a profile in a role without materializing launch files or starting a runtime."""
    try:
        agent = AgentType(role)
    except ValueError:
        raise RegistryError("role is required and must be planner, worker, reviewer or manager") from None
    row = await db_profiles.get_profile(profile_id)
    if row is None:
        raise RegistryNotFound(f"no Agent Profile {profile_id!r}")
    profile = AgentProfile.from_row(row)
    # The Manager spans projects; its launch never receives project context or project layers.
    project_id = None if agent is AgentType.MANAGER else project_id
    settings = replace(await load_agent_settings(role, project_id), profile=profile)
    if problem := profile_problem(profile, agent):
        raise RegistryError(problem)
    role_text, recommended = await _role_prompt(agent)
    notes = ["the Task section is filled in at launch"]
    project = None
    knowledge = AgentKnowledge()
    if project_id is not None:
        project = await db_projects.get_project(project_id)
        if project is None:
            raise RegistryNotFound(f"no project {project_id!r}")
        documents = await db_documents.list_documents(project_id)
        entries = []
        for document in documents:
            if SourceKind(document["source_kind"]) is SourceKind.PATH:
                # Reuse the launch index formatting and missing-file check without materializing files.
                entries.append(_entry(StoredSet(None, {}), project, document))
            else:
                notes.append(f"Project knowledge document {document['title']!r} ({document['id']}) is omitted: "
                             "its path requires a launch-time directory.")
        knowledge = AgentKnowledge(index=clipped_index(entries), documents=len(entries))
        if project.get("operator_notes"):
            notes.append("Operator notes are resolved from the task's working directory at launch.")
    cwd = (project or {}).get("work_dir") or None
    notes.append("Installed skills, native project instruction files and environment context are resolved at launch.")
    if cwd is None:
        notes.append("Injected project instruction files require the task's working directory at launch.")
    else:
        notes.append("Injected project instruction files use the project directory; a task may use another directory.")
    if agent is AgentType.MANAGER:
        from .tiers.manager import TOOLS_CONTRACT

        contract_name, contract = "TOOLS_CONTRACT", TOOLS_CONTRACT
    elif agent is AgentType.PLANNER:
        from .tiers.planner_protocol import reply_contract

        contract_name, contract = "REPLY_CONTRACT", reply_contract()
        notes.append("Planner contract uses default arguments; task-specific tools and options are added at launch.")
    elif agent is AgentType.REVIEWER:
        contract_name, contract = "REVIEW_CONTRACT", REVIEW_CONTRACT
    else:
        contract_name, contract = "WORK_CONTRACT", WORK_CONTRACT
    system_prompt = build_system_prompt(
        harness=settings.harness, custom_prompt=profile.custom_prompt,
        role_id=role, role=role_text, recommended=recommended,
        contract_name=contract_name, contract=contract, project=project, knowledge=knowledge,
    )
    launch = Launch(settings, knowledge=knowledge, role=agent)
    return {
        "profile": {"id": profile.id, "name": profile.name}, "role": role,
        "runtime": launch.runtime.type, "system_prompt_mode": profile.system_prompt_mode.value,
        "custom_prompt_set": bool(profile.custom_prompt.strip()),
        "looper_prompt_sha256": system_prompt.sha256, "prompt_layers": system_prompt.layers,
        "blocks": [asdict(block) for block in launch.runtime.prompt_blocks(launch, system_prompt.text, cwd=cwd)],
        "notes": notes,
    }

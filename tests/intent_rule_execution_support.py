"""Shared synthetic configuration for isolated intent-rule execution checks."""
from __future__ import annotations

import json
from datetime import datetime, timezone

ADAPTER = {"system_id": "review-system", "executable": "/opt/synthetic/verifier", "args": [],
    "allowed_domains": ["review.example"], "route": [{"kind": "id", "name": "repository_id"},
        {"kind": "literal", "value": "mr"}, {"kind": "id", "name": "mr_id"}],
    "operator_id": "review-operator", "credential_env": {}, "env": {}}
URL = "https://review.example/repository/mr/7"


async def configure(pool, directory, *, cap=50):
    from looper.db import intent_rules as store
    from uuid import uuid4
    await pool.execute("INSERT INTO projects(id,name,work_dir,auto_task_daily_cap) VALUES('review','Review',$1,$2)",
                       str(directory), cap)
    definition = {"roles": [], "profiles": {}, "name": "Review", "planner_instructions": "Review the verified MR with the project workflow."}
    await pool.execute("INSERT INTO workflows(key,name,definition) VALUES('review','Review',$1::jsonb)", json.dumps(definition))
    await pool.execute("INSERT INTO intent_rule_template_contracts(workflow_key,version,workflow_digest,input_kind,comment_mode,approve_requires_confirmation,withdraw_requires_confirmation,allow_merge,allow_reviewer_changes) "
        "VALUES('review',1,$1,'verified_mr','direct',true,true,false,false)", __import__('hashlib').sha256(
            json.dumps(definition, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
    async with pool.acquire() as conn, conn.transaction():
        await store.lock_admission(conn)
        await store.lock_config(conn)
        await store.update_config(conn, generation=1, adapter=ADAPTER, discovery_sources=[{
            "id":"discovery", "system_id":"review-system", "enabled":True, "operator_key":"operator",
            "private_target":{"channel_id":"telegram:synthetic","chat":"operator-chat"},"migration_state":"shared_entry"}],
            activation_acceptance=[], now=datetime.now(timezone.utc))
        await store.append_rule_version(conn, {"id":str(uuid4()),"version":1,"enabled":True,"position":1,
            "intent_type":"mr_review","scope":{"kind":"all"},"conditions":[
                {"kind":"internal_mr_open","allowed_domains":["review.example"]},
                {"kind":"mr_repository_in_allowlist","repository_ids":["repository"]},
                {"kind":"operator_is_reviewer"}],"action":"create_task","project_id":"review","template_key":"review"},
            edited_by="operator")
        await conn.executemany("INSERT INTO system_settings(key,value) VALUES($1,$2)", [
            ("intents.rules.enabled","true"),("intents.enabled","true"),("delivery.enabled","true"),
            ("user.timezone","UTC")])


def fact(revision="head-a", state="open"):
    return verified_fact({"schema_version":1,"status":"verified","facts":{
        "exists":True,"state":state,"reviewers":["review-operator"],"repository":"repository", "mr_id":"7",
        "revision":revision,"canonical_url":URL}}, ADAPTER)



def verified_fact(value, config):
    """Build synthetic adapter facts without exposing a product verification entry."""
    from looper.agents.intent_rule_verifier import VerifiedFact, _response, parse_target
    target = parse_target(value["facts"]["canonical_url"], config)
    result = _response(json.dumps(value).encode(), target, config)
    assert isinstance(result, VerifiedFact)
    return result

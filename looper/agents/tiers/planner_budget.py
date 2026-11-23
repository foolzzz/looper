"""Spend waits are control state; no hold text is sent to the Planner model."""
from __future__ import annotations

from functools import wraps
import logging

from ..message import MessageType, MessageEnvelope

from ...db import tasks as db_tasks
from .. import cost_guard, cost_guard_settings, cost_holds
from ..agent_settings import AgentType, Launch, load_agent_settings
from ..project_execution import TaskBudgetAction


logger = logging.getLogger(__name__)


class Held(Exception):
    """The event saved its result and is waiting for an operator's spend decision."""


async def _launch(planner, plan, kind, step=None, same_attempt=False):
    role = AgentType(kind)
    task_role = None
    if step is not None:
        version = step.role_version if same_attempt else None
        key = step.role if role is AgentType.WORKER or step.reviews else None
        if role is AgentType.REVIEWER and not step.reviews:
            item = planner._reviewing_role(step)
            key = item.role if item else None
            version = item.version if item and same_attempt else None
        if key:
            chosen = await planner._role(key, version)
            task_role = (chosen.key, chosen.version) if chosen else None
    config = await load_agent_settings(role, plan.project_id or None, planner._task_id,
                                       step.step_id if step else None, task_role=task_role, wait=False)
    return Launch(config, role=role)


async def hold(planner, plan, admission):
    scope = admission.scope
    if scope.scope is cost_guard.BudgetScope.TASK and planner._execution.on_task_budget is not TaskBudgetAction.ASK:
        await cost_guard.release_queued(planner._task_id)
        await planner._stop_at_budget(plan, admission.spent_usd, scope.limit_usd)
        return
    _, created = await cost_holds.create_hold(scope.scope, scope.scope_ref, admission.currency,
        scope.limit_usd, admission.spent_usd, task_id=planner._task_id, estimate_usd=admission.estimate_usd)
    # Save results before waiting; existing running Workers keep sending their results normally.
    try:
        await planner._save(plan, *plan.steps, plan_row=True)
    except Exception:
        logger.exception('cannot save the plan of held task %s', planner._task_id)
    if created:
        await planner._activity('phase', 'Task waiting for an operator decision at its spend cap')


async def within(planner, plan, run_kind='planner', step=None, same_attempt=False):
    try:
        if cost_guard.current().config.mode is not cost_guard_settings.Mode.ENFORCE:
            if cost_holds.needs_service() and plan.status not in ('stopping', 'completed', 'failed'):
                row = await db_tasks.get_task(planner._task_id)
                if row is not None and row['status'] == 'awaiting_budget':
                    return False
            return None
        return await _enforced(planner, plan, run_kind, step, same_attempt)
    except Held:
        raise
    except Exception:
        cost_guard.current().problem = 'Task admission failed; existing task limits remain active.'
        logger.exception('cannot check spend admission for task %s', planner._task_id)
        return None


async def _enforced(planner, plan, run_kind, step, same_attempt):
    closing = plan.status in ('stopping', 'completed', 'failed')
    # Failed preparation never leaves a phantom reservation for an undispatched step.
    guard = cost_guard.current()
    for reservation in list(guard._pending.values()):
        if reservation.dispatch_task_id != planner._task_id:
            continue
        dispatch = plan.step(reservation.dispatch_step_id)
        if dispatch is None or dispatch.status not in ('dispatched', 'completed'):
            guard.abandon(reservation)
    row = await db_tasks.get_task(planner._task_id)
    if not closing and row is not None and row['status'] == 'awaiting_budget':
        return False
    launch = await _launch(planner, plan, run_kind, step, same_attempt)
    admission = await cost_guard.task_gate(planner._task_id, run_kind, launch, reserve=step is not None,
                                           step_id=step.step_id if step else None)
    if admission.decision is cost_guard.Decision.ADMIT:
        if admission.problem:
            return None
        if admission.reservation is not None:
            admission.reservation.dispatch_task_id = planner._task_id
            admission.reservation.dispatch_step_id = step.step_id
        return True
    if not closing:
        await hold(planner, plan, admission)
    return False


def turn(method):
    @wraps(method)
    async def checked(planner, *args, **kwargs):
        if (cost_guard.current().config.mode is cost_guard_settings.Mode.ENFORCE or cost_holds.needs_service()) and planner._plan is not None:
            if await within(planner, planner._plan) is False:
                if planner._plan.status in ('stopping', 'completed', 'failed'):
                    from .planner import PlannerTurnError
                    raise PlannerTurnError('Spend admission refused the closing turn')
                raise Held()
        return await method(planner, *args, **kwargs)
    return checked


def event(method):
    @wraps(method)
    async def checked(planner, *args, **kwargs):
        try:
            if args:
                envelope = args[0]
                plan = planner._plan
                if envelope.message_type is MessageType.SYSTEM_EVENT and envelope.payload.get('type') == 'cost_dispatch_held':
                    await refused(planner, envelope)
                    return None
                if ((cost_guard.current().config.mode is cost_guard_settings.Mode.ENFORCE or cost_holds.needs_service())
                        and envelope.message_type is MessageType.TASK_RESUME and plan is not None and plan.status == 'planning'):
                    row = await db_tasks.get_task(planner._task_id)
                    if row is not None and row['status'] == 'awaiting_budget':
                        return None
            return await method(planner, *args, **kwargs)
        except Held:
            return None
        except cost_guard.Denied as exc:
            if exc.admission is None or planner._plan is None:
                raise
            await hold(planner, planner._plan, exc.admission)
    return checked


async def resume(planner):
    plan = planner._plan
    if plan is None:
        return
    await planner._read_execution()
    if plan.status == 'planning' and not plan.steps:
        await planner._plan_and_dispatch(MessageEnvelope.create(source=planner.agent_address,
            target=planner.agent_address, message_type=MessageType.TASK_REQUEST,
            payload={'requirement': plan.requirement, 'work_dir': plan.work_dir, 'goal_id': plan.goal_id,
                     'project_id': plan.project_id}, correlation_id=plan.correlation_id))
        return
    for step in list(plan.steps):
        if step.status == 'deciding':
            await planner._decide_again(plan, step)
        elif step.status == 'completed' and not step.reviewer_id:
            await planner._spawn_reviewer(plan, step)
    await planner._dispatch_ready(plan)


async def refused(planner, envelope):
    plan, data = planner._plan, envelope.payload
    if plan is None or plan.status not in ('executing', 'verifying'):
        return
    step = plan.step(data['step_id'])
    if step is None:
        return
    review = data['review']
    current = step.reviewer_id if review else step.worker_id
    if current != envelope.source.agent_id:
        return  # a late refusal cannot reset a newer dispatch
    step.reviewer_id = None
    if review and not step.reviews:
        step.status = 'completed'
    else:
        step.worker_id, step.status = None, 'pending'
    await planner._save(plan, step)
    if cost_guard.current().config.mode is not cost_guard_settings.Mode.ENFORCE:
        await planner._dispatch_ready(plan)
        return
    scope = data['scope']
    await hold(planner, plan, cost_guard.Admission(
        cost_guard.Decision.ASK, cost_guard.Decision.ASK,
        scope=cost_guard.Scope(cost_guard.BudgetScope(scope['scope']), scope['scope_ref'], scope['limit_usd']),
        currency=cost_guard.Currency(data['currency']), spent_usd=data['spent_usd'], estimate_usd=data['estimate_usd']))

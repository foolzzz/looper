"""A launch refused after dispatch is a control event, never a Worker report for the model."""
from .agent_settings import AgentType
from .message import MessageEnvelope, MessageType, worker_address


async def refused(pool, worker, reply, admission):
    worker.status = 'stopped'
    await pool._bus.publish(MessageEnvelope.create(source=worker_address(worker.worker_id), target=reply.target,
        message_type=MessageType.SYSTEM_EVENT, correlation_id=reply.correlation_id, metadata=reply.metadata,
        payload={'type': 'cost_dispatch_held', 'step_id': worker.task_id,
                 'review': worker.role is AgentType.REVIEWER, 'scope': admission.scope.view(),
                 'currency': admission.currency.value, 'spent_usd': admission.spent_usd,
                 'estimate_usd': admission.estimate_usd}))

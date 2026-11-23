"""Operator-only task-quality alert settings; never exposed to agents or chat clients."""
from .. import metrics_alerts
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _INT, _NULL_STR, _array, _object
from ..ops import ErrorCode, OpError

RULE = _object({"rule": {"enum": [r.value for r in metrics_alerts.AlertRule]},
                "state": {"enum": [s.value for s in metrics_alerts.RuleState]},
                "sent_on": _NULL_STR, "tier": _INT}, closed=True)
VIEW = _object({"enabled": _BOOL, "last_checked": _NULL_STR, "rules": _array(RULE)}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("metrics_alerts.get", "POST", "/api/v1/metrics_alerts/get",
              "Read task-quality alert settings and the last daily check.",
              "Read task-quality alert settings and the last daily check. Alerts are off by default.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # Reads only Looper's cached alert settings.
    Operation("metrics_alerts.update", "POST", "/api/v1/metrics_alerts/update",
              "Turn task-quality alerts on or off.",
              "Turn task-quality alerts on or off. Null restores the disabled default. "
              "Daily alerts go to the alert chat; quiet hours apply when delivery is on.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # Stores Looper's switch; no message is sent by this operation.
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id == "metrics_alerts.update":
        return (f"Set metrics.alerts.enabled to {str(bool(arguments['enabled'])).lower()}? "
                "This enables or stops daily alerts to the alert chat and hourly missing-attempt alerts; "
                "quiet hours and the daily cap apply when delivery is on.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "metrics_alerts.get":
        return metrics_alerts.view()
    try:
        return await metrics_alerts.update(arguments["enabled"])
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None

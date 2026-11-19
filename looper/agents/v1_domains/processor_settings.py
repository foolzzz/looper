"""The processor's system setting: whether it refuses to run while the machine's managed hooks would see the messages
it is given (``processor.REFUSE_KEY``)."""

from .. import processor
from ..api_v1 import Operation, _array, _object, _BOOL, _STR
from ..ops import ErrorCode, OpError

MANAGED_HOOKS = {"anyOf": [_object({"source": _STR, "events": _array(_STR), "readable": _BOOL}, closed=True),
                           {"type": "null"}]}
PROCESSOR_SETTINGS = _object({"refuse_with_managed_hooks": _BOOL, "managed_hooks": MANAGED_HOOKS}, closed=True)
SCHEMAS = {"ProcessorSettings": PROCESSOR_SETTINGS}
RESULTS = {}


def _op(name, description, request, result, risk="read"):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=risk == "high")


OPERATIONS = (
    _op("processor_settings.get", "Read whether the processor refuses to run while managed hooks are present, and "
                                  "the managed hooks its runtime has.",
        _object({}, closed=True), PROCESSOR_SETTINGS),
    _op("processor_settings.update", "Change whether the processor refuses to run while managed hooks are present; "
                                     "null restores the default (false).",
        _object({"refuse_with_managed_hooks": {"type": ["boolean", "null"]}}, closed=True), PROCESSOR_SETTINGS, "high"),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "processor_settings.update":
        return None
    refuse = bool(arguments["refuse_with_managed_hooks"])
    return (f"Set {processor.REFUSE_KEY} to {str(refuse).lower()}? "
            + ("Processor runs are refused while the machine's managed hooks are present."
               if refuse else "Processor runs go ahead while the machine's managed hooks are present, and those hooks "
                              "see the messages it processes."))


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "processor_settings.get":
        return await processor.settings_view()
    try:
        return await processor.update_settings(arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None

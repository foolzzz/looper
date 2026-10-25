"""Full model results for existing ops and ordinary model lookup."""

from .. import profile_registry
from ..api_v1 import Operation, _array, _object, _STR
from ..ops.config import answered
from .registry import MODEL

MODEL_RESULT = _object({"model": MODEL, "warnings": _array(_STR)}, closed=True)
RESULTS = {
    "models.list": _object({"models": _array(MODEL)}, closed=True),
    "models.create": MODEL_RESULT,
    "models.update": MODEL_RESULT,
    "models.delete": _object({"deleted": _STR}, closed=True),
}
OPERATIONS = (
    Operation("models.get", "POST", "/api/v1/models/get", "Read one saved model.",
              "Read one saved model. Includes its settings, prices, usage and warnings.",
              _object({"model": _STR}, closed=True), MODEL_RESULT, risk="read"),
)


async def run(server, operation, arguments, principal, headers):
    result = await answered(profile_registry.get_model(arguments["model"]))
    return {**result, "warnings": result.get("warnings", [])}

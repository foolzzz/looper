"""Full model results for existing ops and ordinary model lookup."""

from .. import profile_registry
from ..model_names import MODEL_NAME_SCHEMA
from ..api_v1 import Operation, _array, _object, _STR
from ..ops.config import answered, _MODEL_SETTINGS
from .registry import API_PROTOCOL, MODEL

MODEL_RESULT = _object({"model": MODEL, "warnings": _array(_STR)}, closed=True)
ADD_ENTRY = _object({"name": MODEL_NAME_SCHEMA, "protocol": API_PROTOCOL, **_MODEL_SETTINGS}, ["name", "protocol"], closed=True)
ADD_RESULT = _object({"models": _array(MODEL), "skipped": _array(_object({
    "name": _STR, "protocol": API_PROTOCOL, "reason": _STR}, closed=True)), "warnings": _array(_STR)}, closed=True)
RESULTS = {
    "models.list": _object({"models": _array(MODEL)}, closed=True),
    "models.create": MODEL_RESULT,
    "models.update": MODEL_RESULT,
    "models.delete": _object({"deleted": _STR}, closed=True),
}
OPERATIONS = (
    Operation("models.add_many", "POST", "/api/v1/models/add_many", "Add several models to one provider.",
              "Add several models to one provider. "
              "Add confirmed models atomically. Omitted price fields stay null when Looper's built-in table prices "
              "that field for the name, otherwise default to 0.0; explicit prices including null are kept. Existing "
              "exact name/protocol pairs and repeated entries are skipped. An invalid entry refuses the entire request.",
              _object({"provider_id": _STR, "models": {**_array(ADD_ENTRY), "minItems": 1,
                  "maxItems": profile_registry.ADD_MODELS_MAX}}, closed=True),
              ADD_RESULT, risk="write", operator_only=True),
    Operation("models.get", "POST", "/api/v1/models/get", "Read one saved model.",
              "Read one saved model. Includes its settings, prices, usage and warnings.",
              _object({"model": _STR}, closed=True), MODEL_RESULT, risk="read"),
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "models.add_many":
        return await answered(profile_registry.add_models(arguments["provider_id"], arguments["models"]))
    result = await answered(profile_registry.get_model(arguments["model"]))
    return {**result, "warnings": result.get("warnings", [])}

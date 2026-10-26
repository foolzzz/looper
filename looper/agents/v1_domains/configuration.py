"""Portable configuration transfer for dashboard and client identities."""

import asyncio

from .. import api_v1, config_imports, config_transfer
from ..api_v1 import Operation, _object, _STR
from ..ops import ErrorCode, OpError

# Reuse the version branches as well as properties inspected by older callers.
CONFIG_DOCUMENT = {**api_v1.CONFIG_DOCUMENT, "title": "ConfigDocument"}
CONFIG_ABOUT = _object(dict.fromkeys(("format", "looper_version", "exported_at", "secrets"), _STR), [])
CONFIG_PLAN = api_v1._PLAN
UPLOAD = _object({**api_v1.RESULTS["config.import_preview"]["properties"], "file": CONFIG_ABOUT})
SCHEMAS = {"ConfigDocument": CONFIG_DOCUMENT, "ConfigAbout": CONFIG_ABOUT, "ConfigImportPlan": CONFIG_PLAN,
           "ManagerConfig": api_v1.MANAGER_CONFIG, "TemplateConfig": api_v1.TEMPLATE_CONFIG}
RESULTS = {}
_ID = _object({"import_id": {"type": "string", "minLength": 1}}, closed=True)
OPERATIONS = (
    Operation("config.import_upload", "POST", "/api/v1/config/import_upload", "Upload and preview a configuration.",
              "Upload and preview a configuration. Stream a raw portable document; no configuration is written.",
              CONFIG_DOCUMENT, UPLOAD, risk="read"),
    Operation("config.import_cancel", "POST", "/api/v1/config/import_cancel", "Cancel a staged configuration import.",
              "Cancel a staged configuration import. Only this caller's staged file is removed.",
              _ID, _object({"deleted": _STR}), risk="write"),
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id != "config.import_cancel":
        raise OpError(ErrorCode.INVALID, "upload requires the streaming connection handler")
    if server._config_imports is None:
        raise OpError(ErrorCode.UNAVAILABLE, "the server has no runtime root")
    try:
        return await server._config_imports.cancel(arguments["import_id"], owner=principal.key)
    except config_imports.ImportNotFound as exc:
        raise OpError(ErrorCode.NOT_FOUND, str(exc)) from None
    except config_imports.ImportConflict as exc:
        raise OpError(ErrorCode.CONFLICT, str(exc)) from None


async def receive(server, reader, length, principal, headers):
    """Called before the general body buffer; only the private staging file receives body bytes."""
    try:
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise OpError(ErrorCode.INVALID, "the configuration upload requires application/json")
        if server._config_imports is None:
            raise OpError(ErrorCode.UNAVAILABLE, "the server has no runtime root")
        result = await server._config_imports.receive(reader, length, owner=principal.key,
            policy=principal.kind,
            validate=lambda data: api_v1.validate_arguments(CONFIG_DOCUMENT, data))
        return api_v1.OK, {"status": "ok", "result": result}
    except OpError as exc:
        return api_v1.error(exc.code, str(exc))
    except config_transfer.ConfigFileError as exc:
        return api_v1.error(ErrorCode.INVALID, str(exc))
    except (config_imports.ImportConflict, config_transfer.SchemaChangePending) as exc:
        return api_v1.error(ErrorCode.CONFLICT, str(exc))
    except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
        return api_v1.error(ErrorCode.INVALID, "the configuration upload was interrupted")
    except Exception:
        return api_v1.error(ErrorCode.UNAVAILABLE, "the import failed; try again later")

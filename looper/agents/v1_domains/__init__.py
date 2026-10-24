"""Dashboard domains register by adding their own public module to this package.

Export OPERATIONS, RESULTS and the run/stream callbacks used by those operations.
Private modules (names starting with ``_``) are ignored. Optional integer ORDER
preserves OpenAPI order; modules without it follow ordered modules, by name.
High-risk operations also need the impact callback checked by the API registry.
"""

import importlib as _importlib
import pkgutil as _pkgutil


def _discover():
    modules = [_importlib.import_module(f"{__name__}.{name}")
               for name in sorted(info.name for info in _pkgutil.iter_modules(__path__)
                                  if not info.name.startswith("_"))]
    ids, paths = {}, {}
    for module in modules:
        for export in ("OPERATIONS", "RESULTS"):
            if not hasattr(module, export):
                raise ValueError(f"{module.__name__}: missing required export: {export}")
        if hasattr(module, "ORDER") and type(module.ORDER) is not int:
            raise ValueError(f"{module.__name__}: ORDER must be an integer")
        for operation in module.OPERATIONS:
            callback = "stream" if operation.stream else "run"
            if not callable(getattr(module, callback, None)):
                raise ValueError(f"{module.__name__}: missing required export: {callback}")
            if operation.operation_id in ids:
                raise ValueError(f"duplicate operationId {operation.operation_id!r}: "
                                 f"{ids[operation.operation_id]} and {module.__name__}")
            route = (operation.method, operation.path)
            if route in paths:
                raise ValueError(f"duplicate path {route!r}: {paths[route]} and {module.__name__}")
            ids[operation.operation_id] = module.__name__
            paths[route] = module.__name__
    return tuple(sorted(modules, key=lambda module: (
        not hasattr(module, "ORDER"), getattr(module, "ORDER", 0), module.__name__)))


_DOMAINS = _discover()
MODULES = tuple(module.__name__.rsplit(".", 1)[-1] for module in _DOMAINS)

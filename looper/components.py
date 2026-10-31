"""Component manifests read from immutable Git objects, wheels or loaded files."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import posixpath
import subprocess
import sys
import tomllib
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path

CONFIG = Path(__file__).with_name("components.toml")
SEAT_SOURCES = ("looper/agents/ops/__init__.py", "looper/agents/room.py",
                "looper/agents/ops/room.py", "looper/agents/ops/vault.py")


@dataclass(frozen=True)
class Manifest:
    version: str
    loaded_at: str
    components: dict[str, str]
    adoption: dict
    verification: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping) -> Manifest:
        if not isinstance(value, Mapping) or not all(key in value for key in
                ("version", "loaded_at", "components", "adoption")):
            raise ValueError("invalid system components manifest")
        components, adoption = value["components"], value["adoption"]
        if (not isinstance(components, dict) or set(components) != {"web", "control_plane", "keeper", "runners"}
                or not all(_is_hash(v) for v in components.values())
                or not isinstance(adoption, dict)
                or set(adoption) != {"run_manifest_version", "work_contract", "review_contract", "seat_tools",
                                     "keeper_protocols"}):
            raise ValueError("invalid system components fingerprints")
        if (type(adoption["run_manifest_version"]) is not int or adoption["run_manifest_version"] < 0
                or not all(_is_hash(adoption[k]) for k in ("work_contract", "review_contract"))
                or not isinstance(adoption["seat_tools"], dict)
                or set(adoption["seat_tools"]) != {"worker", "reviewer"}
                or not all(_is_hash(v) for v in adoption["seat_tools"].values())
                or not isinstance(adoption["keeper_protocols"], list)
                or not all(isinstance(v, str) for v in adoption["keeper_protocols"])):
            raise ValueError("invalid system adoption fingerprints")
        return cls(str(value["version"]), str(value["loaded_at"]), dict(components), dict(adoption),
                   dict(value.get("verification", {})))


def _is_hash(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(path, pattern) and ("**" in pattern or path.count("/") == pattern.count("/"))
               for pattern in patterns)


def check_coverage(paths: Iterable[str], config: dict | None = None) -> None:
    config = config or tomllib.loads(CONFIG.read_text())
    unknown = [path for path in paths if not _matches(path, config["ignore"]["paths"])
               and not any((_matches(path, spec["include"]) and not _matches(path, spec.get("exclude", ())))
                           or _matches(path, spec.get("verify", ()))
                           for spec in config["components"].values())]
    if unknown:
        raise ValueError("unclassified tracked files (classify their top-level directory): " + ", ".join(sorted(unknown)))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint(files: Mapping[str, bytes]) -> str:
    return _sha("".join(f"{path} {_sha(files[path])}\n" for path in sorted(files)).encode())


def _literal(source: bytes, name: str):
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f"target is missing constant {name}")


@lru_cache(maxsize=8)
def _seat_tools(sources: tuple[tuple[str, bytes], ...]) -> dict:
    try:
        result = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("_component_probe.py"))],
                                input=json.dumps({p: b.decode("utf-8") for p, b in sources}), text=True,
                                capture_output=True, timeout=30, check=True)
        listings = json.loads(result.stdout)
        return {role: _sha(json.dumps(listing, sort_keys=True, separators=(",", ":")).encode())
                for role, listing in listings.items()}
    except (subprocess.SubprocessError, ValueError) as exc:
        raise ValueError("cannot read target Worker/Reviewer seat tool contracts") from exc


def _manifest(files: Mapping[str, bytes], *, packaged: bool) -> Manifest:
    config = tomllib.loads(files.get("looper/components.toml", CONFIG.read_bytes()).decode())
    canonical = {}
    for path, data in files.items():
        alias = config.get("aliases", {}).get(path + "/")
        if alias is not None:
            expected = posixpath.relpath(alias.rstrip("/"), posixpath.dirname(path)).encode()
            if data != expected:
                raise ValueError(f"invalid component alias target: {path}")
            continue
        for prefix, destination in config.get("aliases", {}).items():
            if path.startswith(prefix):
                path = destination + path[len(prefix):]
                break
        if path in canonical and canonical[path] != data:
            raise ValueError(f"conflicting component aliases for {path}")
        canonical[path] = data
    files = canonical
    hashes = {}
    for name, spec in config["components"].items():
        patterns = spec.get("verify", ()) if name == "web" and packaged else spec["include"]
        selected = {path: data for path, data in files.items()
                    if _matches(path, patterns) and not _matches(path, spec.get("exclude", ()))}
        if name == "web" and packaged:
            selected = {p.replace("looper/web/dashboard/", "web/dist/", 1): b for p, b in selected.items()}
        hashes[name] = _fingerprint(selected)
    built = {p.replace("looper/web/dashboard/", "web/dist/", 1): b for p, b in files.items()
             if _matches(p, config["components"]["web"]["verify"])}
    contracts = files["looper/agents/system_prompt.py"]
    adoption = {"run_manifest_version": config["adoption"]["run_manifest_version"],
                "work_contract": _sha(_literal(contracts, "WORK_CONTRACT").encode()),
                "review_contract": _sha(_literal(contracts, "REVIEW_CONTRACT").encode()),
                "seat_tools": _seat_tools(tuple((p, files[p]) for p in SEAT_SOURCES)),
                "keeper_protocols": sorted(set(config["adoption"]["keeper_protocols"]))}
    return Manifest(_literal(files["looper/__init__.py"], "__version__"),
                    datetime.now(timezone.utc).isoformat(), hashes, adoption, {"web": _fingerprint(built)})


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True, timeout=30).stdout
    except subprocess.SubprocessError as exc:
        raise ValueError("cannot read target Git tree: " + " ".join(args[:2])) from exc


def manifest_from_git(repo: Path, ref: str = "origin/main") -> Manifest:
    tree = _git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{tree}").strip().decode()
    entries = []
    for entry in _git(repo, "ls-tree", "-rz", tree).split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        _mode, kind, oid = metadata.split()
        if kind == b"blob":
            entries.append((path.decode("utf-8"), oid))
    raw = _git(repo, "cat-file", "--batch", data=b"".join(oid + b"\n" for _, oid in entries))
    files, offset = {}, 0
    for path, _ in entries:
        end = raw.index(b"\n", offset)
        size = int(raw[offset:end].split()[2])
        files[path] = raw[end + 1:end + 1 + size]
        offset = end + size + 2
    return _manifest(files, packaged=False)


def manifest_from_wheel(path: Path) -> Manifest:
    with zipfile.ZipFile(path) as wheel:
        files = {n: wheel.read(n) for n in wheel.namelist() if n.startswith("looper/") and not n.endswith("/")}
    return _manifest(files, packaged=True)


def manifest_from_directory(root: Path) -> Manifest:
    """root is the directory containing looper, never the working directory."""
    files = {}
    for prefix in ("looper", "web/src", "web/dist", "runners"):
        for path in (root / prefix).rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                files[relative] = os.readlink(path).encode()
            elif (path.is_file() and "__pycache__" not in path.parts
                    and "node_modules" not in path.parts and path.suffix != ".pyc"):
                files[relative] = path.read_bytes()
    for path in (root / "web").glob("package*.json"):
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    return _manifest(files, packaged=not (root / "web/src").is_dir())


@lru_cache(maxsize=1)
def loaded_manifest() -> Manifest:
    """One startup snapshot per interpreter, retained when installed files change."""
    return manifest_from_directory(Path(__file__).resolve().parent.parent)

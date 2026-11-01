"""Retain adoptable kept runs and end other leftovers before steps resume."""

import asyncio
import logging
import os
import shutil
import signal
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..db import plans
from ..db.connection import get_pool
from . import proc_tree
from .agent_settings import AgentType
from .kept_run import RunHold
from .keeper import DETACH, GRANT, MANIFEST, STDOUT, RUNS_DIR, STATE, STATE_FORMAT, EndReason, KeeperState, read_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KeeperRecord:
    keeper_pid: int
    keeper_started: str
    child_pid: int | None
    child_started: str | None
    state: KeeperState
    end_reason: EndReason | None
    controller_epoch: int | None
    socket: str | None

    @classmethod
    def read(cls, path: Path) -> "KeeperRecord | None":
        data = read_json(path)
        if data is None or type(data.get("format")) is not int or data["format"] != STATE_FORMAT:
            return None
        try:
            values = {name: data[name] for name in cls.__dataclass_fields__}
            for name in ("keeper_pid", "child_pid", "controller_epoch"):
                value = values[name]
                if value is None and name != "keeper_pid":
                    continue
                if type(value) is not int or value < (0 if name == "controller_epoch" else 1):
                    return None
            for name in ("keeper_started", "child_started", "socket"):
                value = values[name]
                if value is None and name != "keeper_started":
                    continue
                if not isinstance(value, str) or not value.strip():
                    return None
            values["state"] = KeeperState(values["state"])
            if values["end_reason"] is not None:
                values["end_reason"] = EndReason(values["end_reason"])
            if (values["state"] != KeeperState.EXITED
                    and ((values["child_pid"] is None) != (values["child_started"] is None)
                         or (values["state"] == KeeperState.RUNNING and values["child_pid"] is None))):
                return None
            return cls(**values)
        except (KeyError, TypeError, ValueError):
            return None


def live(records: Mapping[str, KeeperRecord]) -> dict[str, bool]:
    starts = proc_tree._start_times([record.keeper_pid for record in records.values()])
    if starts is None:
        raise OSError("cannot read keeper start times")
    return {name: starts.get(record.keeper_pid) == record.keeper_started for name, record in records.items()}


def _recover(run_dir: Path, records_dir: Path) -> KeeperRecord | None:
    # The owner identity alone does not tie an agent record to a particular run.
    matches = []
    for path in records_dir.glob("*.json"):
        data = read_json(path)
        if data is None or data.get("run_dir") != str(run_dir.resolve()):
            continue
        if any(type(data.get(name)) is not int or data[name] < 1 for name in ("pid", "server_pid")):
            return None
        if any(not isinstance(data.get(name), str) or not data[name].strip()
               for name in ("started", "server_started")):
            return None
        if path.name != f"{data['pid']}.json":
            return None
        matches.append(KeeperRecord(data["server_pid"], data["server_started"], data["pid"], data["started"],
                                    KeeperState.RUNNING, None, None, None))
    return matches[0] if len(matches) == 1 else None


class Verdict(StrEnum):
    CANDIDATE = "candidate"
    END = "end"
    RELEASE = "release"


class Reason(StrEnum):
    NO_DETACH = "no_detach"
    EPOCH_MISMATCH = "epoch_mismatch"
    KEEPER_ENDED = "keeper_ended"
    CONTRACT_CHANGED = "contract_changed"
    NOT_HELD = "not_held"
    NO_RECORD = "no_record"
    LOG_SHORT = "log_short"
    NOT_ADOPTED = "not_adopted"
    NOT_ADOPTABLE = "not_adoptable"
    KEEPER_DEAD = "keeper_dead"
    BAD_FILES = "bad_files"


async def _running(pid: int, started: str) -> bool:
    starts = await asyncio.to_thread(proc_tree._start_times, [pid])
    if starts is None:
        raise OSError("cannot read kept run start times")
    return starts.get(pid) == started


def _check_owner(record: KeeperRecord, run_dir: Path) -> None:
    if proc_tree.spawns_fenced():
        raise OSError("the control plane is fenced")
    current = KeeperRecord.read(run_dir / STATE)
    if (current is not None and record.controller_epoch is not None and current.controller_epoch is not None
            and current.controller_epoch > record.controller_epoch):
        raise OSError("a newer control plane holds the keeper")


async def _signal_keeper(record: KeeperRecord, sig: signal.Signals, run_dir: Path) -> None:
    # Recheck ownership and identity after the await, immediately before each signal.
    if await _running(record.keeper_pid, record.keeper_started):
        _check_owner(record, run_dir)
        try:
            os.kill(record.keeper_pid, sig)
        except ProcessLookupError:
            pass


async def end_keeper(record: KeeperRecord, run_dir: Path) -> None:
    await _signal_keeper(record, signal.SIGTERM, run_dir)
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        state = read_json(run_dir / STATE)
        if ((state or {}).get("state") == KeeperState.EXITED
                or not await _running(record.keeper_pid, record.keeper_started)):
            break
        await asyncio.sleep(0.1)
    await _end_child(record, run_dir)
    await _signal_keeper(record, signal.SIGKILL, run_dir)


async def _end_child(record: KeeperRecord, run_dir: Path) -> None:
    if record.child_pid is not None and record.child_started is not None:
        if await _running(record.child_pid, record.child_started):
            _check_owner(record, run_dir)
            await proc_tree.end_orphan(record.child_pid, record.child_started, label=f"kept run {run_dir.name}")


@dataclass(frozen=True)
class Candidate:
    run_dir: Path
    run_id: str
    hold: RunHold
    record_id: int
    grant: dict | None


@dataclass
class StartScan:
    candidates: list[Candidate] = field(default_factory=list)
    ended: Counter[Reason] = field(default_factory=Counter)
    released: int = 0
    unidentified: int = 0

    @property
    def record_ids(self) -> list[int]:
        return [candidate.record_id for candidate in self.candidates]


def _candidate_reason(path: Path, record: KeeperRecord, alive: bool, epoch: int,
                      manifest: dict | None, hold: Mapping | None, running: set[int]) -> Reason | None:
    if manifest is None or type(manifest.get("manifest_version")) is not int or manifest["manifest_version"] != 1:
        return Reason.BAD_FILES
    detach = read_json(path / DETACH)
    if detach is None:
        return Reason.NO_DETACH
    consumed = (detach.get("consumed") or {}).get("stdout") if isinstance(detach.get("consumed"), dict) else None
    if (type(detach.get("format")) is not int or detach["format"] != 1
            or type(detach.get("epoch")) is not int or detach["epoch"] < 0
            or type(consumed) is not int or consumed < 0
            or type(detach.get("stdin_offset")) is not int or detach["stdin_offset"] < 0
            or not isinstance(detach.get("at"), str)):
        return Reason.BAD_FILES
    if detach["epoch"] != record.controller_epoch or detach["epoch"] >= epoch:
        return Reason.EPOCH_MISMATCH
    if record.state == KeeperState.EXITED:
        if record.end_reason != EndReason.EXITED:
            return Reason.KEEPER_ENDED
    elif not alive:
        return Reason.KEEPER_DEAD
    try:
        if (path / STDOUT).stat().st_size < consumed:
            return Reason.LOG_SHORT
    except OSError:
        return Reason.LOG_SHORT
    from .. import components
    if manifest.get("adoption") != components.loaded_manifest().adoption:
        return Reason.CONTRACT_CHANGED
    if type(manifest.get("record_id")) is not int or manifest["record_id"] not in running:
        return Reason.NO_RECORD
    if hold is None:
        return Reason.NOT_HELD
    return None


async def scan_at_start(runtime_root: Path, *, epoch: int | None = None,
                        resumable: Collection[str] = (), pool=None) -> StartScan:
    scan = StartScan()
    runs = runtime_root / RUNS_DIR
    if not runs.exists():
        return scan
    records = {path: KeeperRecord.read(path / STATE) for path in sorted(runs.iterdir()) if path.is_dir()}
    bad_files = {path for path, record in records.items() if record is None}
    for path in bad_files:
        records[path] = _recover(path, runtime_root / proc_tree.RECORDS_DIR)
    # Finish the whole liveness preflight before ending keepers or deleting any directory.
    alive = await asyncio.to_thread(live, {path.name: record for path, record in records.items()
                                         if record is not None})
    manifests = {path: read_json(path / MANIFEST) for path in records} if epoch is not None else {}
    holds = await plans.run_holds([path.name for path in records], pool=pool) if epoch is not None else {}
    ids = [data["record_id"] for data in manifests.values()
           if data is not None and type(data.get("record_id")) is int]
    running = set()
    if ids:
        running = {row["id"] for row in await (pool or get_pool()).fetch(
            "SELECT id FROM llm_records WHERE status = 'running' AND id = ANY($1::bigint[])", ids)}
    verdicts = {}
    for path, record in records.items():
        if record is None:
            logger.error("cannot identify the keeper of %s; directory left in place and will be scanned again "
                         "at the next start", path)
            scan.unidentified += 1
        elif path in bad_files:
            verdicts[path] = (Verdict.END, Reason.BAD_FILES)
        elif epoch is not None:
            hold = holds.get(path.name)
            if hold is not None and hold["task_id"] not in resumable:
                hold = None
            reason = _candidate_reason(path, record, alive[path.name], epoch, manifests[path], hold, running)
            grant = read_json(path / GRANT)
            if reason is None and (manifests[path].get("room_seat") or grant is not None):
                from .ops.room import validate_grant

                try:
                    validate_grant(grant, run_id=path.name, task_id=hold["task_id"])
                except ValueError:
                    reason = Reason.BAD_FILES
            if reason is None:
                role = AgentType.WORKER if hold["status"] == "dispatched" else AgentType.REVIEWER
                scan.candidates.append(Candidate(path, path.name, RunHold(hold["task_id"], hold["id"], role),
                                                 manifests[path]["record_id"], grant))
                verdicts[path] = (Verdict.CANDIDATE, None)
            else:
                verdicts[path] = (Verdict.END, reason)
        elif record.state == KeeperState.EXITED or not alive[path.name]:
            verdicts[path] = (Verdict.RELEASE, Reason.KEEPER_DEAD)
        else:
            verdicts[path] = (Verdict.END, Reason.NOT_ADOPTABLE)
    await asyncio.gather(*((end_keeper(records[path], path) if verdict == Verdict.END
                            else _end_child(records[path], path)) for path, (verdict, _) in verdicts.items()
                            if verdict != Verdict.CANDIDATE))
    for path, (verdict, reason) in verdicts.items():
        if verdict == Verdict.CANDIDATE:
            continue
        shutil.rmtree(path)
        if verdict == Verdict.END:
            scan.ended[reason] += 1
        else:
            scan.released += 1
    logger.info("kept runs at start ended=%s released=%d unidentified=%d candidates=%d",
                {reason.value: count for reason, count in scan.ended.items()}, scan.released,
                scan.unidentified, len(scan.candidates))
    return scan

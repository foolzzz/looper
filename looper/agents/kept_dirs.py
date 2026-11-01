"""End leftover keepers before their steps can be dispatched again at startup."""

import asyncio
import logging
import os
import shutil
import signal
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from . import proc_tree
from .keeper import RUNS_DIR, STATE, STATE_FORMAT, EndReason, KeeperState, read_json

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
    END = "end"
    RELEASE = "release"


class Reason(StrEnum):
    NOT_ADOPTABLE = "not_adoptable"
    KEEPER_DEAD = "keeper_dead"
    BAD_FILES = "bad_files"


async def _running(pid: int, started: str) -> bool:
    starts = await asyncio.to_thread(proc_tree._start_times, [pid])
    if starts is None:
        raise OSError("cannot read kept run start times")
    return starts.get(pid) == started


async def _signal_keeper(record: KeeperRecord, sig: signal.Signals) -> None:
    # Recheck immediately before each signal, so a reused pid is left alone.
    if await _running(record.keeper_pid, record.keeper_started):
        try:
            os.kill(record.keeper_pid, sig)
        except ProcessLookupError:
            pass


async def end_keeper(record: KeeperRecord, run_dir: Path) -> None:
    await _signal_keeper(record, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        state = read_json(run_dir / STATE)
        if ((state or {}).get("state") == KeeperState.EXITED
                or not await _running(record.keeper_pid, record.keeper_started)):
            break
        await asyncio.sleep(0.1)
    await _end_child(record, run_dir)
    await _signal_keeper(record, signal.SIGKILL)


async def _end_child(record: KeeperRecord, run_dir: Path) -> None:
    if record.child_pid is not None and record.child_started is not None:
        if await _running(record.child_pid, record.child_started):
            await proc_tree.end_orphan(record.child_pid, record.child_started, label=f"kept run {run_dir.name}")


@dataclass
class StartScan:
    ended: Counter[Reason] = field(default_factory=Counter)
    released: int = 0
    unidentified: int = 0


async def scan_at_start(runtime_root: Path) -> StartScan:
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
    verdicts = {}
    for path, record in records.items():
        if record is None:
            logger.error("cannot identify the keeper of %s; directory left in place and will be scanned again "
                         "at the next start", path)
            scan.unidentified += 1
        elif path in bad_files:
            verdicts[path] = (Verdict.END, Reason.BAD_FILES)
        elif record.state == KeeperState.EXITED or not alive[path.name]:
            verdicts[path] = (Verdict.RELEASE, Reason.KEEPER_DEAD)
        else:
            verdicts[path] = (Verdict.END, Reason.NOT_ADOPTABLE)
    await asyncio.gather(*((end_keeper(records[path], path) if verdict == Verdict.END
                            else _end_child(records[path], path)) for path, (verdict, _) in verdicts.items()))
    for path, (verdict, reason) in verdicts.items():
        shutil.rmtree(path)
        if verdict == Verdict.END:
            scan.ended[reason] += 1
        else:
            scan.released += 1
    logger.info("kept runs at start ended=%s released=%d unidentified=%d",
                {reason.value: count for reason, count in scan.ended.items()}, scan.released, scan.unidentified)
    return scan

"""Standalone, standard-library-only supervisor for one scheduled command."""

import datetime
import json
import os
import re
import select
import signal
import subprocess
import sys
import time


FORMAT = 1
TAIL_CHARS = 4000
TAIL_READ_BYTES = TAIL_CHARS * 4
REDACT_MIN_CHARS = 4
PIPE_DRAIN_SECONDS = 2.0
SELECT_SECONDS = 1.0
CHUNK = 65536
POLL_SECONDS = 0.2

_terminated = None
_PS_ENV = {"LC_ALL": "C", "TZ": "UTC", "PATH": "/bin:/usr/bin"}


def _record_signal(signum, _frame):
    global _terminated
    _terminated = signum


def parse_env_file(path) -> tuple[dict[str, str], str | None]:
    try:
        with open(path, encoding="utf-8") as source:
            lines = source.readlines()
    except OSError as exc:
        return {}, f"env_file {path} is not readable: {exc.strerror}"
    except UnicodeError:
        return {}, f"env_file {path} is not readable: invalid UTF-8"
    values = {}
    for number, line in enumerate(lines, 1):
        line = line.rstrip("\r\n")
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\0" in value:
            return {}, f"env_file {path}: line {number} is not KEY=VALUE"
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        values[key] = value
    return values, None


def _write_all(fd, data):
    remaining = memoryview(data)
    while remaining:
        count = os.write(fd, remaining)
        if count <= 0:
            raise OSError("write made no progress")
        remaining = remaining[count:]


def _write_json(path, data):
    directory, name = os.path.split(path)
    temporary = os.path.join(directory, f".{name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        _write_all(fd, json.dumps(data, ensure_ascii=True).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lstart(pid):
    try:
        listed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], env=_PS_ENV,
            capture_output=True, text=True, timeout=SELECT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode:
        return None
    return " ".join(listed.stdout.split()) or None


def _group_alive(pgid):
    try:
        listed = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,stat="], env=_PS_ENV,
            capture_output=True, text=True, timeout=SELECT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if listed.returncode:
        return True
    for line in listed.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[1] == str(pgid) and not fields[2].startswith("Z"):
            return True
    return False


def _killpg(pgid, sig):
    # Only the new session's child pid may be used as its process group id.
    assert pgid != os.getpgrp()
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


class _Log:
    def __init__(self, spec):
        self.path = spec["log_path"]
        self.maximum = spec["log_max_bytes"]
        self.ring_size = spec["log_tail_ring_bytes"]
        if self.maximum < 0 or self.ring_size < 0:
            raise ValueError("log byte limits must be nonnegative")
        self.total = 0
        self.written = 0
        self.dropped = 0
        self.ring = bytearray()
        self.error = None
        self.fd = None

    def open(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

    def _write(self, data):
        if self.error is None:
            try:
                _write_all(self.fd, data)
            except OSError as exc:
                self.error = f"{type(exc).__name__}: {exc}"[:300]

    def consume(self, data):
        self.total += len(data)
        count = min(len(data), self.maximum - self.written)
        self._write(data[:count])
        self.written += count
        self.ring.extend(data[count:])
        excess = max(0, len(self.ring) - self.ring_size)
        if excess:
            del self.ring[:excess]
            self.dropped += excess

    def close(self):
        if self.fd is None:
            return
        try:
            if self.total > self.maximum:
                marker = (
                    f"\n[looper] log truncated at {self.maximum // 1048576} MiB: "
                    f"the run wrote {self.total} bytes; {self.dropped} bytes in the middle "
                    f"were dropped; the last {len(self.ring)} bytes follow\n"
                )
                self._write(marker.encode("ascii"))
                self._write(self.ring)
            try:
                os.fsync(self.fd)
            except OSError as exc:
                self.error = self.error or f"{type(exc).__name__}: {exc}"[:300]
        finally:
            os.close(self.fd)
            self.fd = None

    def tail(self, file_values):
        # UTF-8 needs up to four bytes per character; include secrets crossing the tail cut.
        read_bytes = TAIL_READ_BYTES + 4 * max((len(value) for value in file_values.values()), default=0)
        try:
            with open(self.path, "rb") as source:
                source.seek(max(0, os.fstat(source.fileno()).st_size - read_bytes))
                tail = source.read(read_bytes).decode("utf-8", "replace")
        except OSError as exc:
            self.error = self.error or f"{type(exc).__name__}: {exc}"[:300]
            return ""
        for value in sorted(set(file_values.values()), key=len, reverse=True):
            if len(value) >= REDACT_MIN_CHARS:
                tail = tail.replace(value, "[redacted]")
        return tail[-TAIL_CHARS:]


def _run_child(child, pgid, spec, log, deadline):
    end = None
    forwarded = False
    kill_at = None
    killed_at = None
    gone_at = None
    eof = False
    fd = child.stdout.fileno()
    os.set_blocking(fd, False)
    while True:
        # EOF is permanently readable; sleep instead of spinning while a child lives.
        wait = min(SELECT_SECONDS, POLL_SECONDS)
        now = time.time()
        if deadline is not None and end is None:
            wait = min(wait, max(0, deadline - now))
        if kill_at is not None and killed_at is None and gone_at is None:
            wait = min(wait, max(0, kill_at - now))
        if eof:
            time.sleep(wait)
        elif select.select([fd], [], [], wait)[0]:
            try:
                data = os.read(fd, CHUNK)
            except BlockingIOError:
                data = None
            if data == b"":
                eof = True
            elif data:
                log.consume(data)

        now = time.time()
        if _terminated is not None and not forwarded:
            _killpg(pgid, _terminated)
            forwarded = True
            end = "runner_terminated"
            kill_at = now + spec["kill_grace_seconds"]
        if deadline is not None and now >= deadline and end is None:
            _killpg(pgid, signal.SIGTERM)
            end = "timed_out"
            kill_at = now + spec["kill_grace_seconds"]
        # A live shell proves its group is alive. Scan ps only after reaping it,
        # so output volume and slow process-table queries do not delay timers.
        returncode = child.poll()
        alive = returncode is None or _group_alive(pgid)
        if kill_at is not None and now >= kill_at and alive and killed_at is None:
            _killpg(pgid, signal.SIGKILL)
            killed_at = now
        if returncode is not None and end is None:
            end = "exited"
            if alive:
                _killpg(pgid, signal.SIGTERM)
                kill_at = now + spec["leftover_grace_seconds"]
        if not alive and gone_at is None:
            gone_at = now
        if child.returncode is not None and not alive and (eof or now - gone_at >= PIPE_DRAIN_SECONDS):
            return end
        # A failed ps is conservatively "alive", but must not hang forever after SIGKILL.
        if killed_at is not None and now - killed_at >= PIPE_DRAIN_SECONDS and alive:
            raise RuntimeError("cannot confirm the command process group ended after SIGKILL")


def main(argv) -> int:
    global _terminated
    _terminated = None
    try:
        directory = argv[0]
        with open(os.path.join(directory, "spec.json"), encoding="utf-8") as source:
            spec = json.load(source)
    except (IndexError, OSError, ValueError) as exc:
        # JSON decoder diagnostics contain location, never the spec's contents.
        print(f"cannot read spec.json: {type(exc).__name__}", file=sys.stderr)
        return 2

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _record_signal)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    child = None
    pgid = None
    log = None
    file_values = {}
    started_at = None
    end = "start_failed"
    error = None
    tail = ""
    status = 0

    try:
        if spec["format"] != FORMAT:
            raise ValueError(f"unsupported spec format: {spec['format']!r}")
        env = dict(os.environ)
        if spec["env_file"] is not None:
            file_values, error = parse_env_file(os.path.expanduser(spec["env_file"]))
        env.update(file_values)
        env.update(spec["env"])
        env.update(spec["schedule_env"])
        if error is None and not os.path.isdir(spec["work_dir"]):
            error = f"work_dir {spec['work_dir']} is not a directory"
        if error is None:
            log = _Log(spec)
            try:
                log.open()
            except OSError as exc:
                error = f"cannot open log {spec['log_path']}: {exc}"
        if error is None:
            try:
                child = subprocess.Popen(
                    ["/bin/sh", "-c", spec["command"]], cwd=spec["work_dir"], env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
            except OSError as exc:
                error = f"cannot start command: {exc}"
        if child is not None:
            pgid = child.pid
            assert pgid != os.getpgrp()
            started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            timeout = spec["timeout_seconds"]
            deadline = time.time() + timeout if timeout is not None else None
            _write_json(os.path.join(directory, "runner.json"), {
                "format": FORMAT, "runner_pid": os.getpid(), "runner_started": _lstart(os.getpid()),
                "child_pid": child.pid, "child_started": _lstart(child.pid), "started_at": started_at,
            })
            end = _run_child(child, pgid, spec, log, deadline)
        if log is not None and log.fd is not None:
            log.close()
            tail = log.tail(file_values)
        _write_json(os.path.join(directory, "exit.json"), _exit_data(
            end, child, started_at, log, tail, error))
    except BaseException as exc:
        status = 1
        error = f"{type(exc).__name__}: {exc}"[:300]
        if child is not None:
            try:
                _killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=PIPE_DRAIN_SECONDS)
            except BaseException:
                pass
        try:
            if log is not None and log.fd is not None:
                log.close()
                tail = log.tail(file_values)
            _write_json(os.path.join(directory, "exit.json"), _exit_data(
                "runner_error", child, started_at, log, tail, error))
        except BaseException:
            pass
    finally:
        if child is not None and child.stdout is not None:
            child.stdout.close()
    return status


def _exit_data(end, child, started_at, log, tail, error):
    code = child.returncode if child is not None else None
    return {
        "format": FORMAT, "end": end,
        "exit_code": code if code is not None and code >= 0 else None,
        "signal": -code if code is not None and code < 0 else None,
        "started_at": started_at, "ended_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "log_bytes": log.total if log is not None else 0,
        "log_dropped_bytes": log.dropped if log is not None else 0,
        "log_truncated": log.total > log.maximum if log is not None else False,
        "log_error": log.error if log is not None else None, "tail": tail, "error": error,
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

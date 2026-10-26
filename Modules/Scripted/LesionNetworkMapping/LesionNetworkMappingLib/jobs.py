"""Shared protocol of the lesion network mapping command-line jobs (standard library only).

Every heavy computation runs as ``PythonSlicer -m LesionNetworkMappingLib.<module> ...`` in a
subprocess. This module gives all of them one event protocol, Stop and resume semantics, capped
BLAS threads and the process primitives a long-running job needs (output lock, parent and orphan
watchdogs, priority, keep-awake). Importing it imports neither numpy nor scipy.

A job module looks like this::

    import sys

    if __name__ == "__main__":
        from LesionNetworkMappingLib.jobs import set_blas_threads

        set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

    from LesionNetworkMappingLib import jobs

    def main(argv):
        parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.example")
        jobs.add_common_arguments(parser)
        args = parser.parse_args(argv)
        cancel = jobs.CancelToken(args.cancel_file)
        jobs.emit("start", job="example", argv=argv, threads=args.threads)
        ...
        cancel.raise_if_cancelled(done=k)  # in the loop: exit 3 with a "cancelled" event
        jobs.emit("done", outputs=[...], elapsed=jobs.elapsed())

    if __name__ == "__main__":
        jobs.run_job(main, job="example")

Events: standard output carries one JSON object per line, flushed. Every line has ``event`` (its
type) and ``t`` (seconds since the job started). Fields by type (more fields may be added; readers
ignore the ones they do not know):

- ``start``: job, version, pid, argv, threads (``emit`` adds version and pid when they are missing)
- ``stage``: name, index, total (1-based index of a step of a multi-step job)
- ``progress``: done, total, elapsed, eta (seconds; eta is null until one unit is done)
- ``snapshot``: intermediate results that are valid as they stand (for example maps after k subjects)
- ``warning``: message, code
- ``result``: kind, path (one output of the job, written completely)
- ``done``: outputs, elapsed, run_id (the job finished successfully; the last line)
- ``error``: message, code ("usage", "exception", "locked", ...), and for unexpected exceptions
  type and traceback (the last 5 lines)
- ``cancelled``: done (units completed before the job stopped), reason

Exit codes: 0 done, 1 error, 2 usage error, 3 cancelled (Stop, SIGTERM/SIGINT, or the parent
process is gone). Jobs may add their own: the pipeline uses 4, 5 and 6, and ``OutputLock`` uses 75
(EX_TEMPFAIL) for an output folder locked by another live process.

Stop: the caller creates the file given as ``--cancel-file`` (or sends SIGTERM); the job polls
``CancelToken.cancelled()`` between units, leaves complete outputs only, and exits with code 3.
Resume: with ``--resume`` a job skips the units recorded as done in its ``ResumeState`` file.

Self-test job (used by the GUI job runner tests)::

    python -m LesionNetworkMappingLib.jobs selftest [--seconds S] [--units N] [--fail] [--out DIR]
        [--numpy] [--threads K] [--cancel-file F] [--resume]
"""

import os
import sys

THREAD_ENV_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS")


def set_blas_threads(argv, default=2):
    """Set the BLAS thread-count environment variables from a --threads argument of ``argv``.

    Must run before numpy is imported: BLAS libraries read these variables when they are loaded.
    Without --threads (or with a value that is not a positive integer) ``default`` is used.
    Returns the thread count that was set, or None.
    """
    threads = str(default)
    for index, arg in enumerate(argv):
        if arg == "--threads" and index + 1 < len(argv):
            threads = argv[index + 1]
        elif arg.startswith("--threads="):
            threads = arg.split("=", 1)[1]
    if threads.isdigit() and int(threads) > 0:
        for name in THREAD_ENV_VARS:
            os.environ[name] = threads
        return int(threads)
    return None


if __name__ == "__main__":
    _NUMPY_BEFORE_BLAS = "numpy" in sys.modules
    set_blas_threads(sys.argv[1:])

import argparse
import json
import math
import platform
import secrets
import signal
import socket
import subprocess
import threading
import time
import traceback

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 3
EXIT_LOCKED = 75  # EX_TEMPFAIL

MAX_THREADS_ENV = "SLICER_LNM_MAX_THREADS"

_IS_WINDOWS = sys.platform == "win32"

_clock = {"t0": time.monotonic()}
_output_lock = threading.Lock()
_output = {"broken": False}
_process_cancel = threading.Event()  # set by signals and watchdogs; every CancelToken observes it
_cancel_reason = {"reason": None}
_signals_installed = {"done": False}
_orphan_watchdog = {"thread": None}


# Events


def reset_clock():
    """Start the job clock (the ``t`` of every event) now; ``run_job`` calls it."""
    _clock["t0"] = time.monotonic()


def elapsed():
    """Seconds since the job started."""
    return time.monotonic() - _clock["t0"]


def library_version():
    from .export import LIBRARY_VERSION

    return LIBRARY_VERSION


def _json_default(value):
    if hasattr(value, "item") and callable(value.item):  # numpy scalars
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if hasattr(value, "tolist") and callable(value.tolist):  # numpy arrays
        return value.tolist()
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _finite(value):
    """``value`` with NaN and infinite floats replaced by None (strict JSON has no such numbers) and dictionary keys JSON can hold."""
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key if key is None or isinstance(key, (str, int, float, bool)) else str(key): _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def to_json(obj):
    """Strict single-line JSON of ``obj`` (numpy scalars, paths and sets converted; NaN becomes null)."""
    try:
        return json.dumps(obj, ensure_ascii=False, allow_nan=False, default=_json_default)
    except (ValueError, TypeError):
        return json.dumps(_finite(obj), ensure_ascii=False, allow_nan=False, default=lambda value: _finite(_json_default(value)))


def emit(event, **fields):
    """Print one event as a JSON line on standard output and flush it; return the event dict.

    ``event`` and ``t`` (seconds since the job started) come first; fields named ``event`` or ``t``
    are ignored. A start event also gets ``version`` and ``pid`` when they are not given.
    Thread-safe, and ``t`` never decreases from one line to the next. A closed pipe (the reader is
    gone) never raises: later events are discarded.
    """
    fields.pop("t", None)
    if event == "start":
        fields.setdefault("pid", os.getpid())
        if "version" not in fields:
            try:
                fields["version"] = library_version()
            except Exception:
                fields["version"] = None
    with _output_lock:
        record = {"event": event, "t": round(elapsed(), 3)}
        record.update(fields)
        record["event"] = event
        line = to_json(record) + "\n"
        if _output["broken"]:
            return record
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except (BrokenPipeError, ValueError, OSError):
            _output["broken"] = True
            try:  # no "Exception ignored" message when Python flushes standard output at exit
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
                os.close(devnull)
            except (OSError, ValueError, AttributeError):
                pass
    return record


def progress(done, total, started, **extra):
    """Emit a progress event: ``done`` of ``total`` units, ``started`` = time.monotonic() at the first unit."""
    spent = max(time.monotonic() - started, 0.0)
    eta = spent / done * max(total - done, 0) if done > 0 and total is not None else None
    return emit("progress", done=done, total=total, elapsed=round(spent, 3), eta=None if eta is None else round(eta, 3), **extra)


# Errors


class JobError(Exception):
    """An expected failure: ``run_job`` emits error{message, code} and exits with ``exit_code``."""

    def __init__(self, message, code="error", exit_code=EXIT_ERROR, **fields):
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.fields = fields


class Cancelled(Exception):  # noqa: N818 (a request, not an error)
    """The job was stopped: ``run_job`` emits cancelled{done, reason} and exits with code 3."""

    def __init__(self, done=None, reason=None):
        super().__init__("cancelled")
        self.done = done
        self.reason = reason


class Locked(JobError):
    """The output is locked by another live process (exit code 75)."""

    def __init__(self, message, holder=None):
        super().__init__(message, code="locked", exit_code=EXIT_LOCKED, holder=holder)
        self.holder = holder


# Command line


class JsonArgumentParser(argparse.ArgumentParser):
    """Argument parser without abbreviated options that reports usage errors as a JSON error event (exit code 2).

    Abbreviations are refused because run records keep the arguments as given and reproduce
    scripts rewrite them by their full names.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def print_help(self, file=None):
        # Standard output carries only JSON events: --help goes to standard error (exit code 0, no event).
        super().print_help(sys.stderr if file is None else file)

    def error(self, message):
        self.print_usage(sys.stderr)
        emit("error", code="usage", message=f"Invalid arguments: {message}")
        self.exit(EXIT_USAGE)


def positive_int(text):
    """argparse type: an integer of at least 1."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {text!r}")
    return value


def add_common_arguments(parser, threads_default=2):
    """Add --threads (at least 1), --cancel-file, --resume, --run-root and --parent-run (repeatable) to ``parser``."""
    parser.add_argument("--threads", type=positive_int, default=threads_default, help=f"BLAS threads (default: {threads_default})")
    parser.add_argument("--cancel-file", default=None, help="stop (exit code 3) as soon as this file exists")
    parser.add_argument("--resume", action="store_true", help="skip the units a previous run of the same job completed")
    parser.add_argument("--run-root", default=None, help="study folder or output root whose derivatives/lnm/runs.jsonl records this run")
    parser.add_argument("--parent-run", action="append", default=[], help="id of a run this run derives from (repeatable)")
    return parser


def budget_threads(requested=None):
    """Worker threads for a job: ``requested`` or max(1, CPUs - 1), capped by $SLICER_LNM_MAX_THREADS; at least 1."""
    if hasattr(os, "process_cpu_count"):
        cpus = os.process_cpu_count()
    elif hasattr(os, "sched_getaffinity"):
        cpus = len(os.sched_getaffinity(0))
    else:
        cpus = os.cpu_count()
    threads = requested if requested and requested > 0 else max(1, (cpus or 1) - 1)
    cap = os.environ.get(MAX_THREADS_ENV, "").strip()
    if cap.isdigit() and int(cap) > 0:
        threads = min(threads, int(cap))
    return max(1, int(threads))


# Stop


def _on_signal(signum, frame):
    _cancel_reason["reason"] = _cancel_reason["reason"] or f"signal {signal.Signals(signum).name}"
    _process_cancel.set()


def install_signal_handlers():
    """Turn SIGTERM, SIGINT (and SIGBREAK on Windows) into a cancellation request.

    Only possible in the main thread; returns whether the handlers are installed.
    """
    if _signals_installed["done"]:
        return True
    if threading.current_thread() is not threading.main_thread():
        return False
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                signal.signal(signum, _on_signal)
            except (OSError, ValueError, RuntimeError):
                pass
    _signals_installed["done"] = True
    return True


def reset_cancel():
    """Clear a process-wide cancellation request (signals, watchdogs, request_cancel()), for example between in-process jobs."""
    _cancel_reason["reason"] = None
    _process_cancel.clear()


def request_cancel(reason=None):
    """Ask every CancelToken of this process to stop (as SIGTERM does)."""
    _cancel_reason["reason"] = _cancel_reason["reason"] or reason
    _process_cancel.set()


class CancelToken:
    """Cancellation flag of a job: set when ``path`` exists, a signal arrived, a watchdog fired or cancel() was called.

    Usable wherever the library accepts ``cancel`` (it has ``is_set()`` and is callable).
    Creating a token in the main thread installs the SIGTERM/SIGINT handlers, except inside the
    Slicer application (``signals=None`` and the ``slicer`` module is loaded), whose own handlers stay.
    """

    def __init__(self, path=None, signals=None):
        self.path = os.fspath(path) if path else None
        self._event = threading.Event()
        self.reason = None
        if signals or (signals is None and "slicer" not in sys.modules):
            install_signal_handlers()

    def cancel(self, reason=None):
        self.reason = self.reason or reason
        self._event.set()

    def cancelled(self):
        if self._event.is_set():
            return True
        if _process_cancel.is_set():
            self.reason = self.reason or _cancel_reason["reason"]
            return True
        if self.path and os.path.exists(self.path):
            self.reason = self.reason or "cancel file"
            return True
        return False

    is_set = cancelled

    def __call__(self):
        return self.cancelled()

    def raise_if_cancelled(self, done=None):
        """Raise ``Cancelled(done)`` when the job should stop."""
        if self.cancelled():
            raise Cancelled(done=done, reason=self.reason)


# Resume


def write_json_atomic(path, obj):
    """Write JSON through a temporary file in the same folder and an atomic rename."""
    path = os.fspath(path)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(to_json(obj))
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


class ResumeState:
    """Units of work a job has completed, saved atomically in a JSON file so that --resume can skip them.

    ``data`` is a free dict saved with the units (for example the parameters the units depend on;
    a job compares them before resuming). A missing or unreadable file gives an empty state.
    """

    def __init__(self, path):
        self.path = os.fspath(path)
        self.done = []
        self.data = {}
        self._done = set()
        self.load()

    def load(self):
        state = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        self.done = [str(unit) for unit in state.get("done", []) if unit is not None]
        self._done = set(self.done)
        self.data = state.get("data", {}) if isinstance(state.get("data"), dict) else {}
        return self

    def save(self):
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        write_json_atomic(self.path, {"format": "slicer-lnm-resume", "version": 1, "done": self.done, "data": self.data})

    def is_done(self, unit):
        return str(unit) in self._done

    def mark_done(self, unit, save=True):
        unit = str(unit)
        if unit not in self._done:
            self._done.add(unit)
            self.done.append(unit)
        if save:
            self.save()

    def clear(self):
        """Forget every unit and remove the file."""
        self.done, self._done, self.data = [], set(), {}
        if os.path.exists(self.path):
            os.remove(self.path)


# Processes


def pid_alive(pid):
    """Whether process ``pid`` exists on this host (POSIX: signal 0; Windows: OpenProcess, never os.kill)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        handle, error = _win_open_process(pid, with_error=True)
        if not handle:
            return error == _ERROR_ACCESS_DENIED  # the process exists but cannot be opened
        try:
            return _win_wait(handle, 0)
        finally:
            _win_close(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _kernel32():
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


_ERROR_ACCESS_DENIED = 5


def _win_open_process(pid, with_error=False):
    """Handle that can be waited on (SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION), or None.

    With ``with_error``, return (handle, Windows error code of a failed OpenProcess or None).
    """
    handle, error = None, None
    try:
        import ctypes

        kernel32 = _kernel32()
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, int(pid)) or None
        if handle is None:
            error = ctypes.get_last_error()
    except Exception:
        handle = None
    return (handle, error) if with_error else handle


def _win_wait(handle, timeout_ms):
    """True while the process of ``handle`` is running (WaitForSingleObject times out)."""
    import ctypes

    kernel32 = _kernel32()
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    return kernel32.WaitForSingleObject(handle, int(timeout_ms)) == 0x102  # WAIT_TIMEOUT


def _win_close(handle):
    try:
        import ctypes

        kernel32 = _kernel32()
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle(handle)
    except Exception:
        pass


def start_watchdogs(cancel_token=None, parent_pid=None, orphan=True, interval=1.0):
    """Cancel the job when its parent process is gone; return the daemon thread (None when there is nothing to watch).

    :param cancel_token: the CancelToken to cancel; None cancels every token of the process.
    :param parent_pid: a process to watch (for example the GUI that started the job, --parent-pid).
    :param orphan: also watch the process that started this one. Terminating the PythonSlicer
        launcher does not terminate the Python process it started; on POSIX that process is then
        re-parented, which this detects (os.getppid() changes). Windows waits on the initial parent.
    """
    targets = []
    if parent_pid:
        targets.append(("parent", int(parent_pid)))
    initial_ppid = os.getppid() if orphan else None
    if orphan and initial_ppid and initial_ppid > 1 and (not parent_pid or int(parent_pid) != initial_ppid):
        targets.append(("orphan", initial_ppid))
    if not targets:
        return None

    def cancel(reason):
        if cancel_token is None:
            request_cancel(reason)
        else:
            cancel_token.cancel(reason)

    handles = {}
    if _IS_WINDOWS:
        for kind, pid in list(targets):
            handles[kind], error = _win_open_process(pid, with_error=True)
            if handles[kind] is None and error == _ERROR_ACCESS_DENIED:  # alive but not ours to watch
                targets.remove((kind, pid))
                del handles[kind]
        if not targets:
            return None

    def watch():
        while True:
            for kind, pid in targets:
                if _IS_WINDOWS:
                    gone = handles.get(kind) is None or not _win_wait(handles[kind], 0)
                elif kind == "orphan":
                    gone = os.getppid() != initial_ppid
                else:
                    gone = not pid_alive(pid)
                if gone:
                    for handle in handles.values():
                        if handle:
                            _win_close(handle)
                    cancel(f"{'parent' if kind == 'parent' else 'launcher'} process {pid} is gone")
                    return
            time.sleep(interval)

    thread = threading.Thread(target=watch, name="lnm-job-watchdog", daemon=True)
    thread.start()
    return thread


def set_priority(nice):
    """Lower the priority of this process (POSIX os.nice(nice); Windows BELOW_NORMAL when nice > 0); return whether it changed."""
    try:
        nice = int(nice or 0)
    except (TypeError, ValueError):
        return False
    if nice <= 0:
        return False
    try:
        if _IS_WINDOWS:
            import ctypes

            kernel32 = _kernel32()
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000))  # BELOW_NORMAL_PRIORITY_CLASS
        os.nice(nice)
        return True
    except Exception:
        return False


class KeepAwake:
    """Keep the computer from sleeping while a job runs (a context manager; safe to start and stop more than once).

    macOS runs ``caffeinate -i -w <pid>`` (it also ends with this process); Windows calls
    SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) on the calling thread and resets it
    on stop; Linux does nothing.
    """

    def __init__(self):
        self._process = None
        self._windows = False

    def start(self):
        try:
            if sys.platform == "darwin" and self._process is None:
                self._process = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif _IS_WINDOWS and not self._windows:
                self._windows = bool(_kernel32().SetThreadExecutionState(0x80000000 | 0x00000001))
        except Exception:
            self._process = None
        return self

    @property
    def active(self):
        return self._process is not None or self._windows

    def stop(self):
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                pass
            self._process = None
        if self._windows:
            try:
                _kernel32().SetThreadExecutionState(0x80000000)
            except Exception:
                pass
            self._windows = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


# Output lock


def _host():
    try:
        return socket.gethostname()
    except OSError:
        return platform.node()


class OutputLock:
    """Exclusive lock of an output folder or file, shared by every job that writes there.

    The lock file is created with O_CREAT | O_EXCL and holds {pid, host, token, started}; a daemon
    thread refreshes its modification time every ``heartbeat_s`` seconds. A lock is stale when its
    modification time is older than ``stale_s`` seconds, or when it was taken on this host by a
    process that no longer exists; a stale lock is reclaimed (``reclaimed`` holds its content, and
    the caller should emit a warning). ``acquire()`` raises ``Locked`` (exit code 75) for a live lock.
    """

    def __init__(self, path, heartbeat_s=10, stale_s=60):
        self.path = os.fspath(path)
        self.heartbeat_s = heartbeat_s
        self.stale_s = stale_s
        self.token = secrets.token_hex(8)
        self.reclaimed = None
        self.acquired = False
        self._stop = threading.Event()
        self._thread = None

    def read(self):
        """Content of the current lock file (a dict), or None when there is none or it is unreadable."""
        try:
            with open(self.path, encoding="utf-8") as f:
                holder = json.load(f)
        except (OSError, ValueError):
            return None
        return holder if isinstance(holder, dict) else None

    def is_stale(self, holder=None):
        """Whether the existing lock file is stale (see the class description)."""
        try:
            age = time.time() - os.stat(self.path).st_mtime
        except FileNotFoundError:
            return True
        if age > self.stale_s:
            return True
        holder = self.read() if holder is None else holder
        if holder is None:  # being written right now, or garbage: stale only by age
            return False
        return holder.get("host") == _host() and holder.get("pid") is not None and not pid_alive(holder.get("pid"))

    def _create(self):
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        fd = os.open(self.path, flags, 0o644)
        try:
            content = {"pid": os.getpid(), "host": _host(), "token": self.token, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            os.write(fd, (to_json(content) + "\n").encode("utf-8"))
        finally:
            os.close(fd)

    def _reclaim(self):
        """Remove a stale lock under a short-lived O_EXCL mutex, so that two reclaimers never remove each other's new lock."""
        mutex = self.path + ".reclaim"
        try:
            fd = os.open(mutex, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                if time.time() - os.stat(mutex).st_mtime > self.stale_s:
                    os.remove(mutex)
            except OSError:
                pass
            return False
        os.close(fd)
        try:
            holder = self.read()
            if not self.is_stale(holder):
                return False
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            self.reclaimed = holder or {}
            return True
        finally:
            try:
                os.remove(mutex)
            except OSError:
                pass

    def acquire(self):
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        for attempt in range(20):
            try:
                self._create()
            except FileExistsError:
                holder = self.read()
                if not self.is_stale(holder):
                    if holder is None and attempt < 5:  # created but not yet written
                        time.sleep(0.05)
                        continue
                    who = f" by process {holder.get('pid')} on {holder.get('host')} since {holder.get('started')}" if holder else ""
                    raise Locked(f"{self.path} is locked{who}", holder=holder)
                if not self._reclaim():
                    time.sleep(0.05)
                continue
            self.acquired = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._heartbeat, name="lnm-output-lock", daemon=True)
            self._thread.start()
            return self
        raise Locked(f"{self.path} could not be locked", holder=self.read())

    def _heartbeat(self):
        while not self._stop.wait(self.heartbeat_s):
            holder = self.read()
            if holder is None or holder.get("token") != self.token:
                return  # the lock was reclaimed: never keep another process's lock fresh
            try:
                os.utime(self.path)
            except OSError:
                pass

    def release(self):
        """Stop the heartbeat and remove the lock file when it is still this lock's."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self.acquired:
            holder = self.read()
            if holder is not None and holder.get("token") == self.token:
                try:
                    os.remove(self.path)
                except OSError:
                    pass
            self.acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


# Running a job


def run_job(main, argv=None, job=None, orphan=True, parent_pid=None, exit=True, signals=None):  # noqa: A002 (exit mirrors sys.exit)
    """Run ``main(argv)`` with the job protocol and exit with its code (or return it with exit=False).

    - ``main`` returns None (or 0) on success: exit 0. It may return another exit code.
    - ``Cancelled`` or KeyboardInterrupt: a cancelled{done, reason} event and exit 3.
    - ``JobError``: an error{message, code} event and its exit code (``Locked``: 75).
    - any other exception: an error event with type, message, code "exception" and the last 5
      traceback lines, and exit 1.
    - SystemExit (from argparse, already reported) passes its code through.

    The orphan watchdog runs by default (``orphan``), and ``parent_pid`` adds a parent watchdog:
    when the watched process is gone every CancelToken reports cancellation. SIGTERM and SIGINT
    become cancellation requests, except inside the Slicer application (``signals=None`` and the
    ``slicer`` module is loaded), whose own handlers stay.
    """
    reset_clock()
    argv = list(sys.argv[1:] if argv is None else argv)
    if signals or (signals is None and "slicer" not in sys.modules):
        install_signal_handlers()
    if parent_pid:
        start_watchdogs(None, parent_pid=parent_pid, orphan=False)
    if orphan and _orphan_watchdog["thread"] is None:
        _orphan_watchdog["thread"] = start_watchdogs(None, orphan=True)
    try:
        code = main(argv)
        code = EXIT_OK if code is None else int(code)
    except Cancelled as exc:
        emit("cancelled", done=exc.done, reason=exc.reason or _cancel_reason["reason"], job=job)
        code = EXIT_CANCELLED
    except KeyboardInterrupt:
        emit("cancelled", done=None, reason="interrupted", job=job)
        code = EXIT_CANCELLED
    except JobError as exc:
        emit("error", message=exc.message, code=exc.code, job=job, **exc.fields)
        code = exc.exit_code
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (EXIT_OK if exc.code is None else EXIT_ERROR)
    except Exception as exc:
        lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        tail = [line.rstrip("\n") for line in "".join(lines).splitlines() if line.strip()][-5:]
        emit("error", message=str(exc) or type(exc).__name__, code="exception", type=type(exc).__name__, traceback=tail, job=job)
        code = EXIT_ERROR
    if exit:
        sys.exit(code)
    return code


# Self-test job


def _selftest(argv):
    parser = JsonArgumentParser(prog="python -m LesionNetworkMappingLib.jobs selftest", description="Test job of the LNM job protocol: N units over S seconds.")
    add_common_arguments(parser)
    parser.add_argument("--seconds", type=float, default=2.0, help="total duration (default: 2)")
    parser.add_argument("--units", type=int, default=10, help="number of units (default: 10)")
    parser.add_argument("--fail", action="store_true", help="raise an exception after the first unit")
    parser.add_argument("--out", default=None, help="folder of the resume state and the result file")
    parser.add_argument("--numpy", action="store_true", help="import numpy and multiply two matrices")
    parser.add_argument("--parent-pid", type=int, default=None, help="stop when this process is gone")
    args = parser.parse_args(argv)
    if args.units < 0 or args.seconds < 0:
        parser.error("--units and --seconds must not be negative")
    cancel = CancelToken(args.cancel_file)
    if args.parent_pid:
        start_watchdogs(cancel, parent_pid=args.parent_pid, orphan=False)
    blas = {name: os.environ.get(name) for name in THREAD_ENV_VARS}
    emit("start", job="selftest", argv=list(argv), threads=args.threads, blas=blas, numpy_before_blas=globals().get("_NUMPY_BEFORE_BLAS"), parent_runs=args.parent_run)
    if args.numpy:
        import numpy as np

        a = np.ones((64, 64))
        emit("snapshot", numpy=np.__version__, value=float((a @ a)[0, 0]))
    state = ResumeState(os.path.join(args.out, ".selftest-resume.json")) if args.out else None
    if state is not None and not args.resume:
        state.clear()
    emit("stage", name="units", index=1, total=1)
    started = time.monotonic()
    pause = args.seconds / args.units if args.units else 0.0
    fail_at = min(1, max(args.units - 1, 0))  # after the first unit when there are several
    if args.fail and args.units == 0:
        raise RuntimeError("selftest failure requested")
    done = 0
    for unit in range(args.units):
        cancel.raise_if_cancelled(done=done)
        if state is not None and state.is_done(unit):
            done += 1
            continue
        deadline = time.monotonic() + pause
        while time.monotonic() < deadline:
            cancel.raise_if_cancelled(done=done)
            time.sleep(min(0.02, max(deadline - time.monotonic(), 0)))
        if args.fail and unit == fail_at:
            raise RuntimeError(f"selftest failure requested at unit {unit}")
        if state is not None:
            state.mark_done(unit)
        done += 1
        progress(done, args.units, started)
    outputs = []
    if args.out:
        path = os.path.join(args.out, "selftest.json")
        write_json_atomic(path, {"units": args.units, "done": done})
        emit("result", kind="selftest", path=path)
        outputs.append(path)
    emit("done", outputs=outputs, elapsed=round(elapsed(), 3), run_id=None, units=done)


def main(argv=None):
    """Command-line entry point: ``python -m LesionNetworkMappingLib.jobs selftest ...``; returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "selftest":
        parser = JsonArgumentParser(prog="python -m LesionNetworkMappingLib.jobs", description="LNM job protocol utilities.")
        parser.add_argument("command", choices=["selftest"])
        parser.parse_args(argv[:1])
        return EXIT_USAGE
    return run_job(_selftest, argv[1:], job="selftest", exit=False)


if __name__ == "__main__":
    sys.exit(main())

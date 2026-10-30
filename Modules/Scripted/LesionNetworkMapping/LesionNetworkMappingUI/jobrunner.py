"""Qt job runner of the Map user interface: every LNM command-line job started from the GUI runs through it.

A job is ``PythonSlicer -m <module> <args> --cancel-file <file> --threads <k>`` that speaks the event
protocol of :mod:`LesionNetworkMappingLib.jobs` (one JSON event per output line, Stop through the
cancel file, exit code 3 when cancelled). The runner:

- keeps the GUI responsive: the process is started with ``slicer.util.launchConsoleProcess(blocking=False)``,
  whose reader thread queues the output and whose main-thread timer hands it over line by line;
- runs at most one heavy job at a time (``JobSpec.heavy``); later heavy jobs wait in a queue, light jobs
  start at once;
- caps the threads: max(1, CPUs - 1) for a heavy job, at most the fork-only setting
  ``Studio/LNM/MaxJobThreads``, 1 for a light job; the BLAS variables of the job are set accordingly;
- keeps the last 200 lines that are not events (``Job.logTail``) and writes the whole output to
  ``<runRoot>/logs/<job id>.log``;
- stops a job with ``Job.stop()``: the cancel file first; when the job is still running 3 s later its
  worker process (the pid of its start event, else the launcher's child processes) is terminated, then
  killed, then the PythonSlicer launcher; a job that still has not ended is detached (cancelled). Stop is
  a button, never the Esc key;
- shows the running job in the Studio status chip ("Mapping 34% · 2 min left", "Done",
  "Run failed: <reason>"); without the Studio shell (Classic, tests, no main window) nothing is shown;
- never reports a silent success: a job succeeds only when it exits with 0 after a ``done`` event, every
  output it reported exists and every post-run check (``afterRun`` hooks) passes.

There is one runner per application (:func:`instance`), shared by Map, Draw and Library, so that one heavy
job runs at a time across workspaces. Callbacks are plain Python lists and callables (no Qt signals).
Every method must be called from the main thread.
"""

import collections
import json
import locale
import logging
import math
import os
import shlex
import shutil
import signal
import sys
import time

import qt
import slicer

from LesionNetworkMappingLib import jobs as _jobs  # the job protocol (standard library only)

#: Fork-only setting (SPEC Appendix B, D-S1): the maximum number of threads of a heavy job
MAX_THREADS_SETTING = "Studio/LNM/MaxJobThreads"

#: Time between the cancel file and the termination of a job that is still running
STOP_GRACE_MS = 3000
#: Further time before the PythonSlicer launcher and the worker are terminated hard
STOP_FORCE_MS = 2000

#: Lines kept in ``Job.logTail``
LOG_TAIL_LINES = 200
#: Longest time a written log line stays in the file buffer (while the job writes)
LOG_FLUSH_S = 1.0

#: Shortest time between two status chip updates from progress events (a later update is never lost)
STATUS_INTERVAL_MS = 250

#: Time a "Done" or "Stopped" status chip stays visible
FINISHED_CHIP_MS = 4000

#: Longest failure reason shown in the status chip (the job keeps the full text)
CHIP_REASON_CHARS = 60

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
FINISHED_STATES = (DONE, FAILED, CANCELLED)

_IS_WINDOWS = sys.platform == "win32"


def moduleDir():
    """The folder of the ``LesionNetworkMapping`` module, which holds ``LesionNetworkMappingLib`` (the job's cwd and PYTHONPATH)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def defaultRunRoot():
    """Output root of jobs without a study: ``<Slicer cache>/LNM/runs`` (the temporary folder without an application)."""
    app = getattr(slicer, "app", None)
    cachePath = app.cachePath if app is not None else ""
    if not cachePath:
        import tempfile

        cachePath = tempfile.gettempdir()
    return os.path.join(cachePath, "LNM", "runs")


def pythonExecutable():
    """The application's own Python (``PythonSlicer``) found through PATH, else ``sys.executable`` (SPEC 9.8)."""
    found = shutil.which("PythonSlicer")
    if found:
        return found
    executable = sys.executable or ""
    if os.path.basename(executable).lower().startswith(("pythonslicer", "python")):
        return executable
    raise RuntimeError("PythonSlicer was not found")


def maxJobThreadsSetting():
    """The positive integer of ``Studio/LNM/MaxJobThreads``, or None when it is not set (read only, never written)."""
    value = qt.QSettings().value(MAX_THREADS_SETTING)
    try:
        value = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def formatEta(seconds):
    """Remaining time as shown in the status chip: "< 1 min left", "12 min left", "1 h 20 min left"; "" when unknown."""
    if seconds is None:
        return ""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ""
    if math.isnan(seconds) or seconds < 0:
        return ""
    if seconds < 60:
        return "< 1 min left"
    if round(seconds / 60) < 60:
        return f"{max(1, round(seconds / 60))} min left"
    hours, minutes = int(seconds // 3600), round(seconds % 3600 / 60)
    if minutes == 60:
        hours, minutes = hours + 1, 0
    return f"{hours} h {minutes} min left" if minutes else f"{hours} h left"


def _studioShell():
    """The live Studio shell, or None (Classic, tests, no main window, Studio not installed)."""
    try:
        from StudioLib import shell
    except Exception:
        return None
    try:
        return shell.instance()
    except Exception:
        return None


def _callSafely(callback, *args, what="callback"):
    try:
        return callback(*args)
    except Exception:
        logging.exception(f"Lesion network mapping job runner: {what} failed")
        return None


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8"):
        pass


def _removeFile(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def _terminatePid(pid, hard=False):
    """Terminate process ``pid`` (POSIX SIGTERM, or SIGKILL when ``hard``; Windows TerminateProcess); return whether it was signalled."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid() or not _jobs.pid_alive(pid):
        return False
    if _IS_WINDOWS:
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if not handle:
                return False
            try:
                return bool(kernel32.TerminateProcess(handle, _jobs.EXIT_CANCELLED))
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, signal.SIGKILL if hard else signal.SIGTERM)
        return True
    except OSError:
        return False


def _childPids(pid):
    """The direct child processes of ``pid`` (Linux /proc, macOS pgrep, Windows Toolhelp snapshot); [] when unknown."""
    if _IS_WINDOWS:
        return _windowsChildPids(pid)
    if os.path.isdir("/proc"):
        children = set()
        try:
            for task in os.listdir(f"/proc/{pid}/task"):
                with open(f"/proc/{pid}/task/{task}/children", encoding="ascii") as f:
                    children.update(int(child) for child in f.read().split())
            return sorted(children)
        except (OSError, ValueError):
            pass  # no children files (kernel without CONFIG_PROC_CHILDREN): scan the parent of every process
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as f:
                    fields = f.read().rsplit(")", 1)[-1].split()
                if int(fields[1]) == pid:
                    children.add(int(entry))
            except (OSError, ValueError, IndexError):
                continue
        return sorted(children)
    try:
        import subprocess

        found = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=2, check=False)
        return sorted(int(child) for child in found.stdout.split() if child.isdigit())
    except Exception:
        return []


def _windowsChildPids(pid):
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = (
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260))

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W))
        kernel32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W))
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return []
        children = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32ParentProcessID == pid and entry.th32ProcessID != pid:
                    children.append(int(entry.th32ProcessID))
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return sorted(children)
    except Exception:
        return []


def _processStartTime(pid):
    """Start time of process ``pid`` in clock ticks (Linux), to tell it from a later process with the same pid; else None."""
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as f:
            return int(f.read().rsplit(")", 1)[-1].split()[19])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _descendantPids(pid, limit=64):
    """Every process started by ``pid``, children first (bounded, cycle-safe)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return []
    found, pending = [], [pid]
    while pending and len(found) < limit:
        for child in _childPids(pending.pop(0)):
            if child != pid and child != os.getpid() and child not in found:
                found.append(child)
                pending.append(child)
    return found[:limit]


def _lowerPriority(pid, nice):
    """Lower the priority of process ``pid`` by ``nice`` (POSIX setpriority; Windows BELOW_NORMAL); errors are ignored."""
    try:
        pid, nice = int(pid), int(nice or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or nice <= 0:
        return False
    try:
        if _IS_WINDOWS:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            handle = kernel32.OpenProcess(0x0200 | 0x1000, False, pid)  # PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                return bool(kernel32.SetPriorityClass(handle, 0x00004000))  # BELOW_NORMAL_PRIORITY_CLASS
            finally:
                kernel32.CloseHandle(handle)
        current = os.getpriority(os.PRIO_PROCESS, pid)
        os.setpriority(os.PRIO_PROCESS, pid, min(19, current + nice))
        return True
    except Exception:
        return False


class JobSpec:
    """What to run.

    :param module: Python module run with ``-m`` (for example "LesionNetworkMappingLib.engine").
    :param args: its arguments; the runner appends ``--cancel-file <file> --threads <k>``.
    :param title: shown in the status chip ("Mapping"); default: the module name.
    :param heavy: heavy jobs run one at a time and get the thread budget; light jobs start at once with 1 thread.
    :param kind: free tag of the job ("engine", "normalization", "pipeline", ...), read by post-run checks.
    :param runRoot: folder whose ``logs/`` gets the job log and cancel file; default: the context's
        ``runRoot()``, else ``<Slicer cache>/LNM/runs``.
    :param onEvent: ``onEvent(event)`` for every JSON event of the job (main thread). The runner adds
        ``{"event": "queued", "waitingFor": title}`` when a heavy job has to wait.
    :param onDone: ``onDone(job)`` once the job is finished (``job.state`` is done, failed or cancelled).
    :param env: extra environment variables; a PYTHONPATH here is put after the module folder, and the BLAS
        variables cannot be overridden.
    :param nice: lower the job's priority by this much (POSIX nice; Windows below normal) when > 0.
    :param afterRun: extra post-run checks ``check(job) -> [failure strings]`` for this job only.
    :param threads: requested threads of a heavy job (still capped by the setting); default max(1, CPUs - 1).
    :param statusText: ``statusText(job) -> str`` replaces the default status chip text while running.
    :param context: the ``MapContext`` whose ``afterRun`` hooks, ``runRoot()`` and ``shell()`` apply.
    :param name: short name used in the job id and the log file name; default: ``kind`` or the module's last part.
    """

    def __init__(self, module, args=(), title=None, heavy=True, kind=None, runRoot=None, onEvent=None, onDone=None, env=None,
                 nice=0, afterRun=None, threads=None, statusText=None, context=None, name=None):
        self.module = module
        self.args = [os.fspath(arg) if isinstance(arg, os.PathLike) else str(arg) for arg in args]
        self.title = title or module
        self.heavy = bool(heavy)
        self.kind = kind
        self.runRoot = os.fspath(runRoot) if runRoot else None
        self.onEvent = onEvent
        self.onDone = onDone
        self.env = dict(env or {})
        self.nice = nice
        self.afterRun = list(afterRun or [])
        self.threads = threads
        self.statusText = statusText
        self.context = context
        self.name = name

    def __repr__(self):
        return f"JobSpec({self.module!r}, {self.args!r}, title={self.title!r}, heavy={self.heavy}, kind={self.kind!r})"


class Job:
    """A submitted job. Read its attributes; call ``stop()``; never change it.

    - ``state``: queued, running, done, failed or cancelled; ``exitCode`` once finished;
    - ``startEvent``, ``doneEvent``, ``errorEvent``, ``cancelledEvent``, ``lastEvent``; ``results``
      (result events), ``warnings`` (warning events), ``outputs`` (from the done event);
    - ``progress`` (0..1 or None), ``eta`` (seconds or None), ``stage`` (the last stage event);
    - ``pid`` (the worker, from the start event), ``proc`` (the launched ``Popen``);
    - ``logTail`` (the last lines that are not events), ``logPath``, ``cancelPath``, ``threads``, ``args``;
    - ``failures`` (every reason the job failed) and ``failureReason`` (the first one).
    """

    def __init__(self, runner, spec, jobId):
        self.runner = runner
        self.spec = spec
        self.id = jobId
        self.title = spec.title
        self.state = QUEUED
        self.exitCode = None
        self.proc = None
        self.pid = None
        self.args = []
        self.threads = None
        self.runRoot = None
        self.logPath = None
        self.cancelPath = None
        self.startEvent = None
        self.doneEvent = None
        self.errorEvent = None
        self.cancelledEvent = None
        self.lastEvent = None
        self.results = []
        self.warnings = []
        self.outputs = []
        self.progress = None
        self.eta = None
        self.stage = None
        self.logTail = collections.deque(maxlen=LOG_TAIL_LINES)
        self.failures = []
        self.failureReason = None
        self.stopRequested = False
        self.submittedAt = time.time()
        self.startedAt = None
        self.finishedAt = None
        self._log = None
        self._logFlushedAt = 0.0
        self._stopStep = 0
        self._stopPids = {}  # pid: start time
        self._statusAt = 0.0
        self._statusPending = False

    def __repr__(self):
        return f"<Job {self.id} {self.state}>"

    @property
    def heavy(self):
        return self.spec.heavy

    @property
    def kind(self):
        return self.spec.kind

    def isQueued(self):
        return self.state == QUEUED

    def isRunning(self):
        return self.state == RUNNING

    def isFinished(self):
        return self.state in FINISHED_STATES

    def succeeded(self):
        return self.state == DONE

    def statusText(self):
        """The status chip text of the running job: "Title 34% · 2 min left" (or the spec's ``statusText``)."""
        if callable(self.spec.statusText):
            text = _callSafely(self.spec.statusText, self, what="statusText")
            if text:
                return str(text)
        title = self.title
        stage = self.stage or {}
        if stage.get("name") and (stage.get("total") or 0) > 1:
            title = f"{title} · {stage['name']}"
        text = title
        if self.progress is not None:
            text += f" {int(self.progress * 100)}%"
        eta = formatEta(self.eta)
        if eta:
            text += f" · {eta}"
        return text

    #
    # Stop
    #

    def stop(self):
        """Stop the job: a queued job is removed; a running job gets its cancel file, then is terminated after 3 s.

        A job still running after the termination is killed (2 s later its worker processes, 2 s later the
        launcher) and, 2 s after that, detached: it is reported as cancelled whatever its processes do, so
        a stopped job always frees its place. Returns immediately. ``onDone`` is called once the job is finished, with state "cancelled"
        (or "done" when it finished before it noticed the request).
        """
        if self.state == QUEUED:
            self.runner._dequeue(self)
            self.stopRequested = True
            self._finish(CANCELLED, [])
            return
        if self.state != RUNNING or self.stopRequested:
            return
        self.stopRequested = True
        self._writeLog("# Stop requested: cancel file created")
        try:
            _touch(self.cancelPath)
        except OSError as exc:
            logging.warning(f"Lesion network mapping: could not create the cancel file {self.cancelPath}: {exc}")
            self._escalateStop()  # terminate at once (the job turns SIGTERM into a cancellation)
            return
        qt.QTimer.singleShot(STOP_GRACE_MS, self._escalateStop)

    def _launcherPid(self):
        return self.proc.pid if self.proc is not None else None

    def _launcherAlive(self):
        return self.proc is not None and self.proc.poll() is None

    def _workerPids(self):
        """The processes to signal before the launcher: the reported worker, the launcher's descendants, and those seen before.

        The PythonSlicer launcher does not forward signals to the Python it starts, and that Python keeps the
        output pipe open, so the job only ends once the worker itself is gone, also when it was stopped before
        it reported its pid.
        """
        launcherPid = self._launcherPid()
        pids = [self.pid] if self.pid and self.pid != launcherPid else []
        if self._launcherAlive():
            pids += _descendantPids(launcherPid)
        # Processes seen before stay targets when the launcher died and they were re-parented, unless their
        # pid now belongs to another process
        pids += [pid for pid, started in self._stopPids.items() if _jobs.pid_alive(pid) and started == _processStartTime(pid)]
        unique = list(dict.fromkeys(pid for pid in pids if pid and pid != launcherPid))
        self._stopPids = {pid: self._stopPids.get(pid, _processStartTime(pid)) for pid in unique}
        return unique

    def _escalateStop(self):
        """Terminate a job that ignored its cancel file: first the worker processes, then the launcher; finally detach."""
        if self.state != RUNNING:
            return
        self._stopStep += 1
        if self._stopStep == 1:
            # Gently first: the reported worker, else the launcher's own children (the worker before its start
            # event); the worker's helper processes are left to it until the next step
            workers = self._workerPids()
            launcherPid = self._launcherPid()
            if self.pid and self.pid != launcherPid:
                workers = [self.pid]
            elif self._launcherAlive():
                workers = [pid for pid in _childPids(launcherPid) if pid in workers] or workers
            self._writeLog(f"# Still running after the cancel file: terminating {workers or 'the launcher'}")
            for pid in workers:
                _terminatePid(pid)  # the job turns SIGTERM into a cancellation (exit code 3)
            if not workers and self._launcherAlive():
                self.proc.terminate()  # the launcher is the worker
        elif self._stopStep == 2:
            workers = self._workerPids()
            self._writeLog(f"# Still running: killing {workers or 'the launcher'}")
            for pid in workers:
                _terminatePid(pid, hard=True)
            if self._launcherAlive():
                if workers:
                    self.proc.terminate()
                else:
                    self.proc.kill()
        elif self._stopStep == 3:
            workers = self._workerPids()
            targets = (["the launcher"] if self._launcherAlive() else []) + [str(pid) for pid in workers]
            self._writeLog("# Still running: " + (f"killing {', '.join(targets)}" if targets else "its processes ended, its output is still open"))
            for pid in workers:
                _terminatePid(pid, hard=True)
            if self._launcherAlive():
                self.proc.kill()
        else:
            # The process did not end (or something else keeps its output open): release the job and its
            # heavy slot anyway; late output and the completion callback are ignored once it is finished
            survivors = [pid for pid in [self._launcherPid(), *self._stopPids] if pid and _jobs.pid_alive(pid)]
            self._writeLog(f"# The job did not exit after Stop: detached (processes still present: {survivors or 'none'})")
            logging.warning(f"Lesion network mapping: {self.title} did not exit after Stop; detached" + (f" (processes {survivors})" if survivors else ""))
            if self.proc is not None:
                self.exitCode = self.proc.poll()
            self._finish(CANCELLED, [])
            return
        qt.QTimer.singleShot(STOP_FORCE_MS, self._escalateStop)

    #
    # Output
    #

    def _openLog(self):
        self._log = open(self.logPath, "a", encoding="utf-8", errors="replace")
        self._logFlushedAt = time.monotonic()

    def _writeLog(self, line):
        if self._log is None:
            return
        try:
            self._log.write(line + "\n")
            now = time.monotonic()
            if now - self._logFlushedAt >= LOG_FLUSH_S:  # buffered: a burst of output costs no write per line
                self._log.flush()
                self._logFlushedAt = now
        except (OSError, ValueError):
            self._log = None

    def _closeLog(self):
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    def _onLine(self, line):
        """Main thread: one output line of the process (standard output and error)."""
        if self.isFinished():
            return
        self._writeLog(line)
        text = line.strip()
        if text.startswith("{"):
            try:
                event = json.loads(text)
            except ValueError:
                event = None
            if isinstance(event, dict) and isinstance(event.get("event"), str):
                self._onEvent(event)
                return
        self.logTail.append(line)

    def _onEvent(self, event):
        kind = event["event"]
        statusChanged = False
        if kind == "start":
            self.startEvent = event
            try:
                self.pid = int(event.get("pid"))
            except (TypeError, ValueError):
                self.pid = None
            if self.spec.nice and self.pid:
                _lowerPriority(self.pid, self.spec.nice)
            statusChanged = True
        elif kind == "progress":
            done, total = event.get("done"), event.get("total")
            try:
                self.progress = min(max(float(done) / float(total), 0.0), 1.0) if total else None
            except (TypeError, ValueError, ZeroDivisionError):
                self.progress = None
            self.eta = event.get("eta")
            statusChanged = True
        elif kind == "stage":
            self.stage = event
            self.progress, self.eta = None, None
            statusChanged = True
        elif kind == "result":
            self.results.append(event)
        elif kind == "warning":
            self.warnings.append(event)
        elif kind == "done":
            self.doneEvent = event
            outputs = event.get("outputs") or []
            self.outputs = list(outputs) if isinstance(outputs, (list, tuple)) else [outputs]
        elif kind == "error":
            self.errorEvent = event
        elif kind == "cancelled":
            self.cancelledEvent = event
        self.lastEvent = event
        if statusChanged:
            self._statusChanged(throttled=kind == "progress")
        self._deliver(event)

    def _statusChanged(self, throttled):
        """Update the status chip; progress updates at most every STATUS_INTERVAL_MS, the last one always shown."""
        if throttled:
            waitMs = STATUS_INTERVAL_MS - (time.monotonic() - self._statusAt) * 1000
            if waitMs > 0:
                if not self._statusPending:
                    self._statusPending = True
                    qt.QTimer.singleShot(int(waitMs) + 1, self._pendingStatus)
                return
        self._statusAt = time.monotonic()
        self.runner._updateStatus()

    def _pendingStatus(self):
        self._statusPending = False
        if self.isRunning():
            self._statusAt = time.monotonic()
            self.runner._updateStatus()

    def _deliver(self, event):
        if callable(self.spec.onEvent):
            _callSafely(self.spec.onEvent, event, what=f"onEvent of {self.title}")
        for observer in list(self.runner.jobEvent):
            _callSafely(observer, self, event, what="jobEvent observer")

    def _errorText(self):
        """The reason of a failed process: its error event, else the last output line, else the exit code."""
        if self.errorEvent:
            message = str(self.errorEvent.get("message") or "").strip()
            if message:
                return message
        if self.exitCode == _jobs.EXIT_LOCKED:
            return "the output folder is locked by another job"
        for line in reversed(self.logTail):
            if line.strip():
                return line.strip()
        return f"exit code {self.exitCode}"

    def isPartial(self):
        """Whether a failed job reported a partial result (an error event with ``partial``: some of its items were done)."""
        return self.state == FAILED and bool(self.errorEvent) and bool(self.errorEvent.get("partial")) and bool(self.failures)

    def _onCompleted(self, returnCode):
        """Main thread: the process ended (every output line was delivered before)."""
        if self.isFinished():
            return
        self.exitCode = returnCode
        if returnCode == 0 and self.doneEvent is not None:
            failures = self.runner._postRunFailures(self)
            self._finish(FAILED if failures else DONE, failures)
        elif self.stopRequested or returnCode == _jobs.EXIT_CANCELLED or self.cancelledEvent is not None:
            self._finish(CANCELLED, [])
        elif returnCode == 0:
            self._finish(FAILED, ["the job ended without reporting its results (no done event)"])
        else:
            self._finish(FAILED, [self._errorText()])

    def _finish(self, state, failures):
        self.state = state
        self.failures = [str(failure) for failure in failures]
        self.failureReason = self.failures[0] if self.failures else None
        self.finishedAt = time.time()
        if self.stopRequested and state == CANCELLED and self.cancelledEvent is None:
            # Delivered so that onEvent consumers see one terminal event in every case
            self.cancelledEvent = {"event": "cancelled", "done": None, "reason": "stopped", "synthetic": True}
            self._deliver(self.cancelledEvent)
        summary = f"# {state} (exit code {self.exitCode})"
        if self.failures:
            summary += ": " + "; ".join(self.failures)
        self._writeLog(summary)
        self._closeLog()
        _removeFile(self.cancelPath)
        if state == FAILED:
            logging.warning(f"Lesion network mapping: {self.title} {'finished with failures' if self.isPartial() else 'failed'}: {'; '.join(self.failures)}" + (f" (log: {self.logPath})" if self.logPath else ""))
        self.runner._jobFinished(self)
        if callable(self.spec.onDone):
            _callSafely(self.spec.onDone, self, what=f"onDone of {self.title}")
        for observer in list(self.runner.jobFinished):
            _callSafely(observer, self, what="jobFinished observer")

    def waitForFinished(self, timeoutS=None):
        """Process events until the job is finished; return whether it is. For tests and scripts, never on a GUI path."""
        deadline = None if timeoutS is None else time.monotonic() + timeoutS
        while not self.isFinished():
            if deadline is not None and time.monotonic() > deadline:
                return False
            slicer.app.processEvents()
            time.sleep(0.01)
        return True


class JobRunner:
    """Runs jobs: one heavy job at a time, light jobs at once (see the module description).

    Observer lists (plain callables, called on the main thread): ``jobStarted(job)``,
    ``jobEvent(job, event)``, ``jobFinished(job)``. ``afterRun`` holds post-run checks applied to every
    job: ``check(job) -> [failure strings]``.
    """

    def __init__(self):
        self.jobStarted = []
        self.jobEvent = []
        self.jobFinished = []
        self.afterRun = []
        self._queue = []
        self._running = []
        self._history = collections.deque(maxlen=50)
        self._counter = 0
        self._statusGeneration = 0
        self._statusShown = False
        self._chipText = None
        self._quitConnected = False

    #
    # Submitting
    #

    def submit(self, spec, context=None):
        """Start ``spec`` (or queue it behind the running heavy job); return its ``Job``.

        A job that cannot be started (PythonSlicer missing, the log folder not writable) fails: its
        ``onDone`` is called from the event loop, never from inside ``submit``.
        """
        if context is not None and spec.context is None:
            spec.context = context
        self._connectQuit()
        self._counter += 1
        name = spec.name or spec.kind or spec.module.rsplit(".", 1)[-1]
        safeName = "".join(character if character.isalnum() or character in "-_" else "-" for character in str(name))[:40] or "job"
        job = Job(self, spec, f"{time.strftime('%Y%m%d-%H%M%S')}-{safeName}-{os.getpid()}-{self._counter}")
        self._history.append(job)
        if spec.heavy and (self.currentHeavyJob() is not None or self._queue):
            # First in, first out: also behind heavy jobs about to start (between two jobs, or from an onDone)
            ahead = self.currentHeavyJob() or self._queue[0]
            waitingFor = ahead.title
            self._queue.append(job)
            qt.QTimer.singleShot(0, lambda: job.isQueued() and job._deliver({"event": "queued", "waitingFor": waitingFor, "position": self._queuePosition(job)}))
            if self.currentHeavyJob() is None:
                qt.QTimer.singleShot(0, self._startNext)
        else:
            self._start(job)
        return job

    def _queuePosition(self, job):
        return self._queue.index(job) + 1 if job in self._queue else 0

    def threadsFor(self, spec):
        """Threads of a job: 1 when light; else the request or max(1, CPUs - 1), capped by Studio/LNM/MaxJobThreads."""
        if not spec.heavy:
            return 1
        requested = spec.threads if isinstance(spec.threads, int) and spec.threads > 0 else None
        threads = _jobs.budget_threads(requested)
        cap = maxJobThreadsSetting()
        if cap:
            threads = min(threads, cap)
        return max(1, threads)

    def environment(self, spec, threads):
        """Environment additions of a job: the spec's variables, PYTHONPATH with the module folder first, and the BLAS caps."""
        env = {str(key): str(value) for key, value in spec.env.items() if key != "PYTHONPATH"}
        paths = [moduleDir()]
        paths += [path for path in str(spec.env.get("PYTHONPATH", "")).split(os.pathsep) if path]
        paths += [path for path in os.environ.get("PYTHONPATH", "").split(os.pathsep) if path]
        unique = []
        for path in paths:
            if path not in unique:
                unique.append(path)
        env["PYTHONPATH"] = os.pathsep.join(unique)
        for name in _jobs.THREAD_ENV_VARS:
            env[name] = str(threads)
        env[_jobs.MAX_THREADS_ENV] = str(threads)  # jobs.budget_threads() of nested workers
        env.setdefault("PYTHONUNBUFFERED", "1")
        # The reader decodes with the locale encoding: make the job write the same, replacing what it cannot hold
        env.setdefault("PYTHONIOENCODING", f"{locale.getpreferredencoding(False) or 'utf-8'}:replace")
        return env

    def _start(self, job):
        spec = job.spec
        job.state = RUNNING
        job.startedAt = time.time()
        self._running.append(job)
        try:
            python = pythonExecutable()
            context = spec.context
            job.runRoot = spec.runRoot or (context.runRoot() if context is not None and hasattr(context, "runRoot") else defaultRunRoot())
            logDir = os.path.join(job.runRoot, "logs")
            os.makedirs(logDir, exist_ok=True)
            job.logPath = os.path.join(logDir, f"{job.id}.log")
            job.cancelPath = os.path.join(logDir, f"{job.id}.cancel")
            _removeFile(job.cancelPath)
            job.threads = self.threadsFor(spec)
            job.args = [python, "-m", spec.module, *spec.args, "--cancel-file", job.cancelPath, "--threads", str(job.threads)]
            env = self.environment(spec, job.threads)
            job._openLog()
            job._writeLog(f"# {job.title} ({job.id}), {time.strftime('%Y-%m-%d %H:%M:%S')}, {job.threads} thread(s)")
            job._writeLog("# " + shlex.join(job.args))
            job.proc = slicer.util.launchConsoleProcess(
                job.args, useStartupEnvironment=False, updateEnvironment=env, cwd=moduleDir(), blocking=False,
                logCallback=job._onLine, completedCallback=job._onCompleted)
        except Exception as exc:
            logging.exception(f"Lesion network mapping: could not start {job.title}")
            reason = f"could not start: {exc}"
            qt.QTimer.singleShot(0, lambda: job.isRunning() and job._finish(FAILED, [reason]))
            self._updateStatus()
            return
        if spec.nice:
            _lowerPriority(job.proc.pid, spec.nice)
        for observer in list(self.jobStarted):
            _callSafely(observer, job, what="jobStarted observer")
        self._updateStatus()

    def _dequeue(self, job):
        if job in self._queue:
            self._queue.remove(job)

    def _jobFinished(self, job):
        if job in self._running:
            self._running.remove(job)
        self._updateStatus(finished=job)
        if job.heavy:
            qt.QTimer.singleShot(0, self._startNext)

    def _startNext(self):
        if self.currentHeavyJob() is not None or not self._queue:
            return
        self._start(self._queue.pop(0))

    #
    # Post-run checks
    #

    @staticmethod
    def _missingOutputs(job):
        """Outputs the job reported (done ``outputs``, ``path``, ``json``, ``qa_json``; result ``path``) that do not exist or are empty files."""
        paths = [output for output in job.outputs if isinstance(output, str)]
        done = job.doneEvent or {}
        paths += [done.get(key) for key in ("path", "json", "qa_json") if isinstance(done.get(key), str) and done.get(key)]
        paths += [event.get("path") for event in job.results if isinstance(event.get("path"), str)]
        failures = []
        for path in dict.fromkeys(paths):
            fullPath = path if os.path.isabs(path) else os.path.join(moduleDir(), path)
            if not os.path.exists(fullPath):
                failures.append(f"missing output {path}")
            elif os.path.isfile(fullPath) and os.path.getsize(fullPath) == 0:
                failures.append(f"empty output {path}")
        return failures

    def _postRunFailures(self, job):
        """Failures of a job that exited with 0 after a done event: missing outputs, then every afterRun hook."""
        failures = self._missingOutputs(job)
        context = job.spec.context
        hooks = list(job.spec.afterRun) + list(getattr(context, "afterRun", None) or []) + list(self.afterRun)
        for hook in hooks:
            try:
                result = hook(job)
            except Exception as exc:
                logging.exception("Lesion network mapping: a post-run check failed")
                failures.append(f"post-run check {getattr(hook, '__name__', 'check')} failed: {exc}")
                continue
            if not result:
                continue
            failures += [result] if isinstance(result, str) else [str(item) for item in result if item]
        return failures

    #
    # State
    #

    def currentHeavyJob(self):
        """The running heavy job, or None."""
        for job in self._running:
            if job.heavy:
                return job
        return None

    def runningJobs(self):
        return list(self._running)

    def queuedJobs(self):
        return list(self._queue)

    def recentJobs(self):
        """The last 50 submitted jobs, oldest first."""
        return list(self._history)

    def isBusy(self, heavyOnly=True):
        """True while a heavy job (or any job when ``heavyOnly`` is False) runs or waits."""
        if heavyOnly:
            return self.currentHeavyJob() is not None or bool(self._queue)
        return bool(self._running) or bool(self._queue)

    def ownsStatusChip(self):
        """True while the runner shows a job in the Studio status chip (other code then leaves the chip alone)."""
        return self._statusShown

    def stopAll(self):
        """Stop every queued and running job."""
        for job in list(self._queue) + list(self._running):
            job.stop()

    #
    # Status chip
    #

    def _shell(self, job=None):
        context = job.spec.context if job is not None else None
        if context is not None and hasattr(context, "shell"):
            return context.shell()
        return _studioShell()

    def _statusJob(self):
        """The job shown in the status chip: the running heavy job, else the latest running light job."""
        heavy = self.currentHeavyJob()
        if heavy is not None:
            return heavy
        return self._running[-1] if self._running else None

    def _setChip(self, job, text, level):
        self._chipText = text
        shell = self._shell(job)
        if shell is None:
            return
        try:
            shell.setStatusChip(text, level)
        except Exception:
            logging.exception("Lesion network mapping: could not update the status chip")

    def _hideOwnChip(self, job):
        """Hide the status chip unless other code has shown its own text since (for example "Done · N=1000")."""
        self._statusShown = False
        shell = self._shell(job)
        chip = getattr(getattr(shell, "topBar", None), "statusChip", None) if shell is not None else None
        if chip is not None:
            try:
                if chip.isHidden() or str(chip.text) != str(self._chipText):
                    self._chipText = None  # already hidden, or showing someone else's text
                    return
            except Exception:
                pass  # the chip cannot be read: hide it as before
        self._setChip(job, None, "neutral")

    def _updateStatus(self, finished=None):
        self._statusGeneration += 1
        job = self._statusJob()
        if job is not None:
            self._statusShown = True
            self._setChip(job, job.statusText(), "busy")
            return
        if finished is None:
            return
        if finished.state == FAILED:
            reason = finished.failureReason or "unknown error"
            if len(reason) > CHIP_REASON_CHARS:
                reason = reason[: CHIP_REASON_CHARS - 1] + "…"
            self._statusShown = True
            self._setChip(finished, reason if finished.isPartial() else f"Run failed: {reason}", "warn")  # stays until the next job: never a silent failure
            return
        self._statusShown = True
        self._setChip(finished, "Done" if finished.state == DONE else "Stopped", "ok" if finished.state == DONE else "neutral")
        generation = self._statusGeneration
        qt.QTimer.singleShot(FINISHED_CHIP_MS, lambda: self._clearFinishedStatus(generation, finished))

    def _clearFinishedStatus(self, generation, job):
        if generation != self._statusGeneration or self._statusJob() is not None:
            return
        self._hideOwnChip(job)

    def clearStatus(self):
        """Hide a finished job's status chip (for example once its failure was shown in the card).

        Text that other code set in the chip after the runner's own is left alone, here and when a "Done"
        or "Stopped" chip expires.
        """
        if self._statusJob() is not None:
            return
        self._statusGeneration += 1
        if self._statusShown:
            self._hideOwnChip(None)

    #
    # Application exit
    #

    def _connectQuit(self):
        if self._quitConnected or getattr(slicer, "app", None) is None:
            return
        try:
            slicer.app.connect("aboutToQuit()", self._onAboutToQuit)
            self._quitConnected = True
        except Exception:
            pass

    def _onAboutToQuit(self):
        """Ask every running job to stop (its cancel file): jobs never outlive the application unnoticed."""
        self._queue = []
        for job in list(self._running):
            job.stopRequested = True
            try:
                _touch(job.cancelPath)
            except (OSError, TypeError):
                pass


_instance = None


def instance():
    """The application's JobRunner (created on first use), shared by Map, Draw and Library."""
    global _instance
    if _instance is None:
        _instance = JobRunner()
    return _instance

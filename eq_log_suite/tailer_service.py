"""The tailer, running inside the app.

Follows exactly one log: the one last selected in the Live picker. Selecting
another log stops following the old one and starts on the new one. Nothing
is followed until a log is selected -- there's no folder scanning.

Runs tailer.tail_log_source (the same per-file loop as ever: recording,
alerts, DPS meter, overlay messages) on its own asyncio event loop in a
background thread, so the web app stays responsive while it blocks on
database writes.

Only one tailer may follow logs at a time, in any process: two would each
record every line. A lock file (data/tailer.lock) enforces that, across
copies of the app too.
"""

import asyncio
import os
import threading
from pathlib import Path

from eq_log_suite import db

_service = None
_service_lock = threading.Lock()


def lock_path() -> Path:
    return db.main_path().with_name("tailer.lock")


class TailLock:
    """An exclusive, process-wide lock on data/tailer.lock. Released
    automatically if the process dies, so a crash can't leave it stuck."""

    def __init__(self):
        self._f = None

    def acquire(self) -> bool:
        path = lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        self._f = f
        return True


class _NullBroadcaster:
    """Stand-in when the overlay socket can't be opened -- recording and
    alerts still work, overlay messages just go nowhere."""

    def send(self, obj):
        pass


class TailerService:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.loop = None
        self.thread = None
        self.broadcaster = None
        self.task = None
        self.conn = None
        self.following = None      # path being followed
        self.error = None          # why following isn't possible, if it isn't
        self.overlay_error = None
        self._lock = TailLock()

    def _start_thread(self):
        if self.thread:
            return
        if not self._lock.acquire():
            self.error = ("Another copy of the app is already recording -- close it (or bin/stop-all.sh) "
                          "so this one can. Two would record every line twice.")
            raise RuntimeError(self.error)
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._open_broadcaster())
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, name="eqls-tailer", daemon=True)
        self.thread.start()
        ready.wait(10)

    async def _open_broadcaster(self):
        from eq_log_suite.tailer import OverlayBroadcaster
        if os.name == "nt":
            # The in-game overlay is Linux-only (and so are Unix sockets
            # here) -- nothing to connect, and nothing worth warning about.
            self.broadcaster = _NullBroadcaster()
            return
        try:
            b = OverlayBroadcaster()
            await b.start(self.socket_path)
            self.broadcaster = b
        except (OSError, AttributeError, NotImplementedError) as e:
            # e.g. no Unix sockets on this platform -- the overlay is optional
            self.overlay_error = f"{type(e).__name__}: {e}"
            self.broadcaster = _NullBroadcaster()

    async def _follow(self, log_source: dict):
        from eq_log_suite.tailer import _tail_log_source_isolated
        await self._stop_current()
        self.conn = db.get_connection()
        self.task = asyncio.get_running_loop().create_task(
            _tail_log_source_isolated(self.conn, log_source, self.broadcaster))
        self.following = log_source["file_path"]

    async def _stop_current(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except BaseException:
                pass
            self.task = None
        if self.conn:
            self.conn.close()
            self.conn = None
        self.following = None

    def follow(self, path: str):
        """Follow `path` from its saved position (it was just recorded up to
        there by the selection). Raises RuntimeError if another tailer holds
        the lock."""
        self._start_thread()
        kb = db.get_connection()
        try:
            with kb.cursor() as cur:
                cur.execute("SELECT * FROM log_sources WHERE file_path=%s", (path,))
                log_source = cur.fetchone()
        finally:
            kb.close()
        if not log_source:
            raise RuntimeError(f"{path} hasn't been recorded yet -- select it in the picker first")
        asyncio.run_coroutine_threadsafe(self._follow(log_source), self.loop).result(30)
        self.error = None

    def pause(self) -> str | None:
        """Stops following (e.g. while the database is being rebuilt).
        Returns the path that was being followed, to follow() again after."""
        if not (self.loop and self.task):
            return None
        path = self.following
        asyncio.run_coroutine_threadsafe(self._stop_current(), self.loop).result(30)
        return path

    def status(self) -> dict:
        alive = bool(self.task and not self.task.done())
        return {"following": self.following if alive else None, "error": self.error,
                "stopped": bool(self.following and not alive), "overlay_error": self.overlay_error}


def get() -> TailerService:
    global _service
    with _service_lock:
        if _service is None:
            from eq_log_suite.tailer import DEFAULT_SOCKET_PATH
            _service = TailerService(db.config().get("overlay_socket") or DEFAULT_SOCKET_PATH)
        return _service


def current() -> TailerService | None:
    """The service if this process has started one (no side effects)."""
    return _service

"""Entry point for the packaged app (PyInstaller -- see eq-log-suite.spec)."""
import sys


def _log_to_file():
    """A windowed Windows build has no console: sys.stdout/stderr are None,
    so the first print -- or uvicorn's logger, which calls
    sys.stdout.isatty() -- would crash the app. Send them to app.log in the
    per-user data folder instead, where they're useful for troubleshooting
    (%LOCALAPPDATA%\\EQ Log Suite\\logs\\app.log)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from eq_log_suite import db
    log_dir = db.DATA_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    f = open(log_dir / "app.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or f
    sys.stderr = sys.stderr or f


if __name__ == "__main__":
    _log_to_file()
    from eq_log_suite.desktop import main
    main()

# -*- coding: utf-8 -*-
"""
LiveRecorder Pro — Native Desktop App
=====================================
Wraps the web control panel inside a pywebview native window.
Single .exe when built with PyInstaller (--noconsole / --windowed).

Usage (dev):
    pip install pywebview requests
    python desktop_app.py

Usage (build .exe from parent directory):
    pip install pyinstaller
    pyinstaller --onefile --noconsole ^
        --paths "." ^
        --add-data "recorder_panel.html;." ^
        --name "LiveRecorder" ^
        --distpath "luzhi\dist" ^
        luzhi\desktop_app.py
"""

import sys
import os
import threading
import time
from http.server import HTTPServer

# ---------------------------------------------------------------------------
# CRITICAL: In PyInstaller --noconsole mode, sys.stdout / sys.stderr are None.
# recorder_server.py line 8 calls sys.stdout.reconfigure() which crashes.
# Redirect to devnull BEFORE importing recorder_server.
# ---------------------------------------------------------------------------
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# ---------------------------------------------------------------------------
# Prevent subprocess calls (used by recorder_server._find_ffmpeg) from
# briefly flashing a console window in noconsole mode.
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    import subprocess as _sp
    _original_run = _sp.run
    def _noflask_run(*args, **kwargs):
        """Wrapped subprocess.run that hides console windows."""
        if "startupinfo" not in kwargs:
            kwargs["startupinfo"] = _sp.STARTUPINFO()
            kwargs["startupinfo"].dwFlags |= _sp.STARTF_USESHOWWINDOW
            kwargs["startupinfo"].wShowWindow = _sp.SW_HIDE
        if "creationflags" not in kwargs:
            kwargs["creationflags"] = _sp.CREATE_NO_WINDOW
        return _original_run(*args, **kwargs)
    _sp.run = _noflask_run

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
# Project layout:
#   d:\vs study cc\           ← PARENT_DIR (recorder_server.py, recorder_panel.html)
#   d:\vs study cc\luzhi\     ← SCRIPT_DIR (this file, build scripts)
#
# In PyInstaller onefile mode:
#   sys.frozen is True
#   sys.executable is the .exe path
#   sys._MEIPASS is the temp extraction dir (read-only, contains all bundled files)

if getattr(sys, "frozen", False):
    _IS_FROZEN = True
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    _MEIPASS = sys._MEIPASS
else:
    _IS_FROZEN = False
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    PARENT_DIR = os.path.dirname(SCRIPT_DIR)
    APP_DIR = PARENT_DIR
    _MEIPASS = PARENT_DIR
    if PARENT_DIR not in sys.path:
        sys.path.insert(0, PARENT_DIR)

# ---------------------------------------------------------------------------
# Import the backend module
# ---------------------------------------------------------------------------
import recorder_server  # noqa: E402

# ---------------------------------------------------------------------------
# Override OUTPUT_DIR — use config if set, otherwise default to D drive
# ---------------------------------------------------------------------------
_default_output = "D:/LiveRecorder/recordings"
_stored_dir = recorder_server._config.get("output_dir", "")
if _stored_dir:
    recorder_server.OUTPUT_DIR = _stored_dir
else:
    recorder_server.OUTPUT_DIR = _default_output
os.makedirs(recorder_server.OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PORT = 8766
WINDOW_TITLE = "LiveRecorder Pro"
WINDOW_WIDTH = 820
WINDOW_HEIGHT = 950
MIN_WIDTH = 600
MIN_HEIGHT = 500


# ---------------------------------------------------------------------------
# Error popup — uses Windows MessageBoxW (works 100% with --noconsole)
# ---------------------------------------------------------------------------
def _show_error(title: str, message: str) -> None:
    """Display an error popup that works even in windowed/noconsole mode."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass
    # Fallback: tkinter or stderr
    try:
        import tkinter.messagebox as mb
        mb.showerror(title, message)
    except Exception:
        print(f"\n[{title}] {message}\n", file=sys.stderr)


class DesktopApp:
    """Manages the lifecycle: HTTP server + native window + graceful shutdown."""

    def __init__(self):
        self.httpd: HTTPServer | None = None
        self._shutdown_done = False

    # ---- Server lifecycle --------------------------------------------------

    def start_server(self) -> None:
        """Start the HTTP server and scheduler loop in daemon threads.

        Raises OSError if the port is already in use.
        """
        self.httpd = HTTPServer(("127.0.0.1", PORT), recorder_server.Handler)
        threading.Thread(
            target=self.httpd.serve_forever,
            daemon=True,
            name="http-server",
        ).start()

        threading.Thread(
            target=recorder_server.scheduler_loop,
            daemon=True,
            name="scheduler-loop",
        ).start()

    def stop_server(self) -> None:
        """Signal recording to stop, then shut down the HTTP server.

        Does NOT block — all cleanup happens on a background thread so the
        window can close instantly without stutter.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True

        recorder_server.state["running"] = False
        recorder_server.stop_event.set()

        def _shutdown():
            # Give daemon threads a moment to flush current segment
            time.sleep(0.5)
            if self.httpd:
                try:
                    self.httpd.shutdown()
                except Exception:
                    pass

        threading.Thread(target=_shutdown, daemon=True, name="shutdown").start()

    # ---- Window event handler ----------------------------------------------

    def on_window_closing(self) -> bool:
        self.stop_server()
        return True

    # ---- Main entry point --------------------------------------------------

    def run(self) -> None:
        """Start server, open native window, handle shutdown."""
        # --- Import webview ---
        try:
            import webview
        except ImportError:
            _show_error(
                "Missing Dependency",
                "pywebview is not installed.\n\n"
                "Install it with:\n"
                "    pip install pywebview\n"
                "Or:\n"
                "    pip install -r requirements_desktop.txt"
            )
            sys.exit(1)

        # --- localStorage / cookies 持久化目录 ---
        _storage = "D:/LiveRecorder/userdata"
        os.makedirs(_storage, exist_ok=True)

        # --- Start backend ---
        try:
            self.start_server()
        except OSError as e:
            _show_error(
                "启动失败",
                f"端口 {PORT} 已被占用。\n\n"
                f"请先关闭正在运行的 LiveRecorder 窗口，\n"
                f"或等待几秒后重试。\n\n"
                f"({e})"
            )
            sys.exit(1)

        # --- Create native window ---
        window = webview.create_window(
            title=WINDOW_TITLE,
            url=f"http://localhost:{PORT}",
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            confirm_close=False,
        )

        window.events.closing += self.on_window_closing

        # Block until the window is closed.
        webview.start(debug=False, private_mode=False, storage_path=_storage)

        # Safety net cleanup
        self.stop_server()


def main() -> None:
    try:
        app = DesktopApp()
        app.run()
    except Exception as e:
        _show_error("Unexpected Error", f"LiveRecorder encountered an error:\n\n{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

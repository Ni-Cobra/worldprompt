"""Windows notification-area controls; Linux remains a command-line service."""
from __future__ import annotations

from pathlib import Path
import threading
import webbrowser


class SystemTray:
    def __init__(self, url, shutdown):
        import pystray
        from PIL import Image
        # The round logo, shared with the GUI favicon; pystray scales it to the tray size.
        with Image.open(Path(__file__).with_name("static") / "icon.png") as source:
            picture = source.convert("RGBA")
        self.url, self.shutdown = url, shutdown
        self.ready = threading.Event()
        self.error = None
        self.icon = pystray.Icon("WorldPromptCore", picture, "WorldPrompt Core",
                                 menu=pystray.Menu(
                                     pystray.MenuItem("Open settings", self.open_settings, default=True),
                                     pystray.Menu.SEPARATOR,
                                     pystray.MenuItem("Exit Core", self.exit_core)))
        self.thread = threading.Thread(target=self._run, name="worldprompt-tray", daemon=True)

    def open_settings(self, *_):
        threading.Thread(target=webbrowser.open, args=(self.url,), daemon=True).start()

    def exit_core(self, *_):
        # Never block the Windows message loop while workers and HTTP shut down.
        threading.Thread(target=self.shutdown, name="tray-shutdown", daemon=True).start()

    def _run(self):
        def setup(icon):
            icon.visible = True
            self.ready.set()
        try:
            self.icon.run(setup=setup)
        except Exception as exc:
            self.error = exc
            self.ready.set()

    def start(self):
        self.thread.start()
        if not self.ready.wait(10) or self.error:
            self.stop()
            raise RuntimeError("Could not create the Windows tray icon. Run the console launcher with --no-tray.") from self.error

    def stop(self):
        self.icon.visible = False
        self.icon.stop()
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=3)

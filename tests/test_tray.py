"""Native Windows tray controls."""
import os
import threading

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows notification area")
def test_native_tray_opens_settings_and_dispatches_exit(monkeypatch):
    from worldprompt_core.tray import SystemTray
    import worldprompt_core.tray as module
    opened, exited = threading.Event(), threading.Event()
    urls = []
    def browser(url):
        urls.append(url)
        opened.set()
    monkeypatch.setattr(module.webbrowser, "open", browser)
    tray = SystemTray("http://127.0.0.1:9876", exited.set)
    try:
        tray.start()
        assert tray.icon.visible
        items = list(tray.icon.menu.items)
        assert items[0].text == "Open settings"
        assert items[-1].text == "Exit Core"
        items[0](tray.icon)
        assert opened.wait(3)
        assert urls == ["http://127.0.0.1:9876"]
        items[-1](tray.icon)
        assert exited.wait(3)
    finally:
        tray.stop()
    assert not tray.thread.is_alive()
    assert not tray.icon.visible

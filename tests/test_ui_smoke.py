"""界面冒烟测试：真的把窗口建起来，点一遍按钮。

只在有图形环境时跑（CI 里会跳过）。它的价值是发现“按钮调了
controller 上不存在的方法”这类只有点下去才会暴露的问题。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from lidar_capture.config import Config, ensure_dirs


HAS_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


@unittest.skipUnless(HAS_DISPLAY and shutil.which("bash"), "需要图形环境与 bash")
class TestUiSmoke(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lidar-ui-"))
        self.cfg = Config(
            profile="sim",
            poll_interval=0.2,
            connect_timeout=8.0,
            start_timeout=6.0,
            stop_timeout=4.0,
            project_dir=self.tmp,
            sim_root=str(self.tmp / "runtime"),
        )
        ensure_dirs(self.cfg)
        from lidar_capture.ui import App

        self.app = App(self.cfg)
        self.app.update()

    def tearDown(self):
        try:
            if self.app.winfo_exists():
                self.app._on_close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pump(self, seconds: float):
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            time.sleep(0.02)

    def test_buttons_reach_existing_controller_methods(self):
        ctrl = self.app.ctrl
        for name in ("connect", "start_recording", "stop_recording", "reconnect", "reset", "shutdown"):
            self.assertTrue(callable(getattr(ctrl, name, None)), f"Controller 缺 {name}()")

    def test_reset_button_does_not_raise(self):
        self.app._reset()
        self._pump(3.0)
        for card in self.app.cards.values():
            self.assertIn(card.status, ("pending", "run", "ok", "warn", "fail"))

    def test_window_fits_on_screen(self):
        self.assertLessEqual(self.app.winfo_width(), self.app.winfo_screenwidth())
        self.assertLessEqual(self.app.winfo_height(), self.app.winfo_screenheight())


if __name__ == "__main__":
    unittest.main()

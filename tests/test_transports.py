"""链路构造的单元测试：主要防“真机模式一启动就崩”。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lidar_capture.config import Config
from lidar_capture.transports import (
    LinkError,
    MinicomConsole,
    SerialLink,
    SimLinkAdapter,
    build_link,
)


class TestBuildLink(unittest.TestCase):
    def test_real_profile_tolerates_missing_runtime_dir(self):
        """真机模式没有 runtime 目录，构造链路不能炸。"""
        cfg = Config(profile="real", console="native")
        link = build_link(cfg, None)
        self.assertIsInstance(link, SerialLink)
        self.assertIsNone(link.runtime_dir)
        self.assertIn(cfg.device, link.describe())

    def test_sim_profile_wraps_sim_link(self):
        tmp = Path(tempfile.mkdtemp(prefix="lidar-link-"))
        cfg = Config(profile="sim", sim_root=str(tmp / "runtime"))
        link = build_link(cfg, Path(cfg.sim_root))
        self.assertIsInstance(link, SimLinkAdapter)
        self.assertEqual(link.sim.root, Path(cfg.sim_root))

    def test_real_native_connect_reports_missing_device(self):
        cfg = Config(profile="real", console="native", device="/dev/definitely-not-a-tty")
        link = build_link(cfg, None)
        with self.assertRaises(LinkError):
            link.connect()

    def test_real_minicom_connect_reports_missing_device(self):
        cfg = Config(profile="real", console="minicom", device="/dev/definitely-not-a-tty")
        link = build_link(cfg, None)
        with self.assertRaises(LinkError):
            link.connect()

    def test_minicom_console_opens_own_workdir_when_none(self):
        console = MinicomConsole("/dev/ttyFAKE0", 1500000)
        self.assertTrue(console.workdir.is_dir())
        # 真打开会因为没有设备而失败，这里只验证兜底目录可用
        self.assertEqual(console.device, "/dev/ttyFAKE0")


if __name__ == "__main__":
    unittest.main()

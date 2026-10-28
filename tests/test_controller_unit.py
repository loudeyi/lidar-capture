"""控制台的“少说废话”规则：协议回声过滤 + 只在结论变化时写证据行。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lidar_capture.config import Config  # noqa: E402
from lidar_capture.controller import Controller  # noqa: E402
from lidar_capture.events import EventLog  # noqa: E402


def make_controller() -> Controller:
    tmp = Path(tempfile.mkdtemp(prefix="lidar-unit-"))
    events: list[dict] = []
    ctrl = Controller(Config(profile="sim"), None, EventLog(tmp / "logs"), emit=events.append)
    ctrl.console = []  # type: ignore[attr-defined]
    ctrl._test_events = events  # type: ignore[attr-defined]
    return ctrl


class TestProtocolNoise(unittest.TestCase):
    def setUp(self):
        self.ctrl = make_controller()

    def test_marker_lines_are_noise(self):
        for line in (
            "__B3__",
            "__E3__",
            "__RC3__ 0",
            "__RC3__ 1",
            "__rc=$?; echo __RC3__ $__rc; echo __E3__",
            "root@rk3588:~# __E3__",
            "echo __PING__",
            "__PING__",
            "root@rk3588:~# __PING__",
            "",
            "   ",
        ):
            self.assertTrue(self.ctrl._is_protocol_noise(line), line)

    def test_real_output_survives(self):
        for line in (
            "[1] 101",
            "-rw-rw-r-- 1 root root 1205592 Oct 28 15:27 lidar.pcap",
            "ls: cannot access 'lidar.pcap': No such file or directory",
            "Welcome to Debian GNU/Linux 11 (bullseye)",
            "tcpdump: listening on eth1, link-type EN10MB",
        ):
            self.assertFalse(self.ctrl._is_protocol_noise(line), line)

    def test_quiet_zone_swallows_poll_output(self):
        self.ctrl._quiet += 1
        self.ctrl._publish_line("ls: cannot access 'lidar.pcap': No such file or directory")
        self.ctrl._quiet -= 1
        self.ctrl._publish_line("tcpdump: listening on eth1")
        texts = [e["text"] for e in self.ctrl._test_events if e.get("type") == "text"]
        self.assertEqual(texts, ["tcpdump: listening on eth1\n"])


class TestEvidenceLines(unittest.TestCase):
    def setUp(self):
        self.ctrl = make_controller()

    def _lines(self):
        return [e["text"].strip() for e in self.ctrl._test_events if e.get("type") == "text"]

    def test_one_line_per_conclusion(self):
        ctrl = self.ctrl
        now = 1000.0
        for i in range(4):
            ctrl._last_verdict = "growing"
            ctrl._note_size_change("growing", 1_000_000 * (i + 1), now + i)
        self.assertEqual(self._lines(), [])

        ctrl._note_size_change("stalled", 4_000_000, now + 4.0)
        self.assertEqual(len(self._lines()), 0, "刚变慢还不能算停住")
        for t in (1001.0, 1002.0, 1002.6):
            ctrl.track.add(t, 4_000_000)
        ctrl._note_size_change("stalled", 4_000_000, 1002.8)
        self.assertEqual(self._lines(), ["ll lidar.pcap → 3.81 MB（不再变化）"])

    def test_missing_hint_mentions_file_name(self):
        ctrl = self.ctrl
        for i in range(3):
            ctrl.track.add(100.0 + i, None)
        ctrl._note_size_change("missing", None, 102.0)
        self.assertEqual(self._lines(), ["ll lidar.pcap → —（还没生成）"])


if __name__ == "__main__":
    unittest.main()

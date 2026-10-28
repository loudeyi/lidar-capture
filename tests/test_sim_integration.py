"""端到端：假 3588 + 真状态机。跑的是和实机完全相同的代码路径。

每个用例都在临时目录里跑，互不干扰；结束时会把远端的假 tcpdump 杀掉。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lidar_capture.config import Config  # noqa: E402
from lidar_capture.controller import Controller, parse_size  # noqa: E402
from lidar_capture.events import EventLog  # noqa: E402
from lidar_capture.transports import build_link  # noqa: E402


class SimCase(unittest.TestCase):
    scenario = "normal"
    poll = 0.3

    def make(self, **overrides):
        self.tmp = Path(tempfile.mkdtemp(prefix="lidar-sim-"))
        cfg = Config(
            profile="sim",
            poll_interval=self.poll,
            connect_timeout=10.0,
            start_timeout=overrides.pop("start_timeout", 8.0),
            stop_timeout=overrides.pop("stop_timeout", 6.0),
            sim_scenario=self.scenario,
            sim_rate=overrides.pop("sim_rate", 800_000),
            project_dir=self.tmp,
            sim_root=str(self.tmp / "runtime"),
        )
        for key, value in overrides.items():
            setattr(cfg, key, value)
        self.events = []
        link = build_link(cfg, self.tmp / "runtime")
        ctrl = Controller(cfg, link, EventLog(self.tmp / "logs"), emit=self.events.append)
        ctrl.start()
        self.addCleanup(self._cleanup, ctrl, link)
        self.cfg = cfg
        self.ctrl = ctrl
        self.link = link
        self.runtime = self.tmp / "runtime"
        return ctrl

    def _cleanup(self, ctrl, link):
        ctrl.shutdown()
        ctrl.join(timeout=5)
        try:
            link.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 等状态 ----------
    def wait_state(self, *states, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.ctrl.state in states:
                return True
            time.sleep(0.05)
        self.fail(f"等状态 {states} 超时，当前 {self.ctrl.state}；事件尾部：{self.tail_events()}")

    def tail_events(self, n=12):
        out = []
        for ev in self.events[-n:]:
            if ev.get("type") == "text":
                out.append(("text", ev["text"][-120:]))
            else:
                out.append(tuple(sorted(ev.items(), key=lambda kv: kv[0])))
        return out

    def pcap_size(self):
        path = self.runtime / "lidar.pcap"
        return path.stat().st_size if path.exists() else None


class TestHappyPath(SimCase):
    def test_full_flow(self):
        ctrl = self.make()
        ctrl.connect()
        self.wait_state("dmesg_ok", "ready")
        self.assertEqual(ctrl.steps["connect"]["status"], "ok")
        self.assertEqual(ctrl.steps["dmesg"]["status"], "ok")

        ctrl.start_recording()
        self.wait_state("recording")
        first = self.pcap_size()
        self.assertIsNotNone(first)
        self.assertGreater(first, 0)
        time.sleep(1.0)
        second = self.pcap_size()
        self.assertGreater(second, first)
        self.assertEqual(ctrl.steps["start"]["status"], "ok")

        ctrl.stop_recording()
        self.wait_state("stopped")
        self.assertEqual(ctrl.steps["stop"]["status"], "ok")
        stopped_size = self.pcap_size()
        time.sleep(1.2)
        self.assertEqual(self.pcap_size(), stopped_size, "停止后 pcap 不应再变大")

        logs = Path(self.cfg.logs_dir)
        self.assertTrue((logs / "session.log").exists())
        self.assertTrue((logs / "events.jsonl").exists())
        self.assertTrue((logs / "summary.md").exists())
        summary = (logs / "summary.md").read_text(encoding="utf-8")
        self.assertIn("已停止", summary)

    def test_second_session_overwrites_logs_and_pcap(self):
        ctrl = self.make()
        ctrl.connect()
        self.wait_state("dmesg_ok")
        ctrl.start_recording()
        self.wait_state("recording")
        time.sleep(0.8)
        ctrl.stop_recording()
        self.wait_state("stopped")
        first_summary = (Path(self.cfg.logs_dir) / "summary.md").read_text(encoding="utf-8")
        self.assertIn("已停止", first_summary)

        ctrl.start_recording()
        self.wait_state("recording")
        time.sleep(0.6)
        events = (Path(self.cfg.logs_dir) / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn('"event": "stopped"', events, "新会话的日志应从空白开始")
        self.assertEqual(events.count('"event": "start"'), 1)
        ctrl.stop_recording()
        self.wait_state("stopped")


class TestUnplugReplug(SimCase):
    def test_survives_cable_pull(self):
        ctrl = self.make()
        ctrl.connect()
        self.wait_state("dmesg_ok")
        ctrl.start_recording()
        self.wait_state("recording")

        self.link.sim.disconnect()
        self.wait_state("disconnected", timeout=6)

        self.link.sim.connect()
        ctrl.reconnect()
        self.wait_state("recording", timeout=15)
        self.assertIs(ctrl.proc_running, True)
        self.assertEqual(ctrl.steps["connect"]["status"], "ok")

        ctrl.stop_recording()
        self.wait_state("stopped")
        self.assertEqual(ctrl.steps["stop"]["status"], "ok")


class TestStopStuck(SimCase):
    scenario = "stuck"

    def test_stop_reports_failure(self):
        ctrl = self.make(stop_timeout=3.0)
        ctrl.connect()
        self.wait_state("dmesg_ok")
        ctrl.start_recording()
        self.wait_state("recording")
        ctrl.stop_recording()
        self.wait_state("stop_failed", timeout=15)
        self.assertEqual(ctrl.steps["stop"]["status"], "fail")
        self.assertIn("仍在运行", ctrl.steps["stop"]["detail"])


class TestNoTraffic(SimCase):
    scenario = "no_traffic"

    def test_start_waits_for_growth(self):
        ctrl = self.make(start_timeout=2.0)
        ctrl.connect()
        self.wait_state("dmesg_ok")
        ctrl.start_recording()
        self.wait_state("starting")
        time.sleep(4.0)
        self.assertEqual(ctrl.state, "starting", "没上包时不能显示“录制中”")
        self.assertEqual(ctrl.steps["start"]["status"], "warn")
        self.assertTrue(any(ev.get("type") == "toast" and "没变化" in ev.get("text", "") for ev in self.events))


class TestDmesgFail(SimCase):
    scenario = "dmesg_fail"

    def test_start_blocked_when_dmesg_fails(self):
        ctrl = self.make()
        ctrl.connect()
        self.wait_state("ready", "error")
        self.assertEqual(ctrl.steps["dmesg"]["status"], "fail")
        ctrl.start_recording()
        time.sleep(1.0)
        self.assertNotEqual(ctrl.state, "recording")
        self.assertEqual(self.pcap_size(), None)


class TestParseSize(unittest.TestCase):
    def test_ls_long(self):
        out = "-rw-r--r-- 1 root root 12345678 Oct 28 14:20 lidar.pcap"
        self.assertEqual(parse_size(out), 12345678)

    def test_busybox_style(self):
        out = "-rw-r--r--    1 root     root       2048 Oct 28 14:20 lidar.pcap"
        self.assertEqual(parse_size(out), 2048)

    def test_missing_file(self):
        self.assertIsNone(parse_size("ls: cannot access 'lidar.pcap': No such file or directory"))

    def test_bare_number(self):
        self.assertEqual(parse_size("4096"), 4096)

    def test_empty(self):
        self.assertIsNone(parse_size(""))


if __name__ == "__main__":
    unittest.main()

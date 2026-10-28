import re
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lidar_capture.protocol import (  # noqa: E402
    Console,
    LinkDown,
    RemoteShell,
    Result,
    _parse_marked,
    strip_terminal,
)


class TestStripTerminal(unittest.TestCase):
    def test_ansi_and_crlf(self):
        raw = "\x1b[1;32m__B1__\x1b[0m\r\nhello\r\n"
        self.assertEqual(strip_terminal(raw), "__B1__\nhello\n")

    def test_minicom_nul_padding(self):
        raw = "__PING__\x00\x00\x00\r\n"
        self.assertEqual(strip_terminal(raw), "__PING__\n")

    def test_osc_and_backspace(self):
        raw = "\x1b]0;title\x07abc\x08d\n"
        self.assertEqual(strip_terminal(raw), "abd\n")

    def test_lone_cr(self):
        self.assertEqual(strip_terminal("a\rb"), "a\nb")


class FakeShell(Console):
    """把 RemoteShell 写进来的命令“执行”掉，模拟 3588 的 shell。"""

    name = "fake"

    def __init__(self, outputs=None, echo_prompt="root@rk3588:~# ", break_after=None):
        self.outputs = outputs or {}
        self.prompt = echo_prompt
        self.break_after = break_after
        self.buf = ""
        self.lock = threading.Lock()
        self.calls = []
        self._broken = False
        self._pending = None
        self._pending_rc = 0

    def _emit(self, text):
        with self.lock:
            self.buf += text

    def write(self, text):
        if self._broken:
            raise LinkDown("数据线已断开")
        for line in text.replace("\r", "\n").split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            self.calls.append(stripped)
            self._emit(f"{self.prompt}{stripped}\n")

            match = re.fullmatch(r"echo __B(\d+)__", stripped)
            if match:
                self._pending = match.group(1)
                self._emit(f"__B{self._pending}__\n")
                continue
            if stripped == "echo __PING__":
                self._emit("__PING__\n")
                continue
            if stripped.startswith("__rc=$?;"):
                ident = self._pending or "0"
                self._pending = None
                self._emit(f"__RC{ident}__ {self._pending_rc}\n__E{ident}__\n")
                if self.break_after is not None and len(self.calls) >= self.break_after:
                    self._broken = True
                continue
            if self._pending is not None:
                out, rc = self.outputs.get(stripped, ("", 0))
                self._pending_rc = rc
                if out:
                    self._emit(f"{out}\n")

    def read(self, timeout):
        if self._broken:
            raise LinkDown("数据线已断开")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if self.buf:
                    data, self.buf = self.buf, ""
                    return data
            time.sleep(0.01)
        return ""

    def alive(self):
        return not self._broken


def wait_until(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class TestRemoteShell(unittest.TestCase):
    def make_shell(self, outputs=None, **kwargs):
        console = FakeShell(outputs, **kwargs)
        shell = RemoteShell(console)
        shell.start()
        self.addCleanup(shell.stop)
        return shell

    def test_run_captures_stdout_and_rc(self):
        shell = self.make_shell({"ll lidar.pcap": ("-rw-r--r-- 1 root root 4096 Oct 28 14:20 lidar.pcap", 0)})
        result = shell.run("ll lidar.pcap", timeout=3)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.rc, 0)
        self.assertIn("4096", result.out)

    def test_run_reports_nonzero_rc(self):
        shell = self.make_shell({"pgrep -x tcpdump": ("", 1)})
        result = shell.run("pgrep -x tcpdump", timeout=3)
        self.assertEqual(result.rc, 1)
        self.assertFalse(result.ok)

    def test_timeout_when_no_marker(self):
        console = FakeShell()
        console.write = lambda text: None  # 3588 吞掉命令
        shell = RemoteShell(console)
        shell.start()
        self.addCleanup(shell.stop)
        result = shell.run("anything", timeout=0.3)
        self.assertTrue(result.timed_out)

    def test_marker_ignores_echoed_command_line(self):
        """回声里那行也含 __B/__E，不能把它当结果。"""
        shell = self.make_shell({"echo hi": ("hi", 0)})
        result = shell.run("echo hi", timeout=3)
        self.assertEqual(result.out, "hi")

    def test_wait_ready(self):
        shell = self.make_shell()
        self.assertTrue(shell.wait_ready(timeout=1.0))

    def test_link_down_propagates(self):
        console = FakeShell(break_after=1)
        shell = RemoteShell(console)
        shell.start()
        self.addCleanup(shell.stop)
        with self.assertRaises(LinkDown):
            for _ in range(50):
                shell.run("echo x", timeout=0.5)
                time.sleep(0.01)


class TestParseMarked(unittest.TestCase):
    def test_last_marker_wins(self):
        buf = (
            "root@rk3588:~# echo __B1__; { ll ; } 2>&1\n"
            "__B1__\nfirst\n__RC1__ 0\n__E1__\n"
            "__B1__\nsecond\n__RC1__ 0\n__E1__\n"
        )
        out, rc = _parse_marked(buf, "__B1__", "__E1__", 1)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "second")

    def test_partial_end_marker_is_not_a_result(self):
        buf = "__B1__\nhello\n__RC1__ 0\n__E1__"
        self.assertIsNone(_parse_marked(buf, "__B1__", "__E1__", 1))

    def test_prompt_prefix_tolerated(self):
        buf = "__B1__\nhi\n__RC1__ 0\nroot@rk3588:~# __E1__\n"
        out, rc = _parse_marked(buf, "__B1__", "__E1__", 1)
        self.assertEqual(out, "hi")
        self.assertEqual(rc, 0)


class TestResult(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(Result(rc=0, out="", raw="").ok)
        self.assertFalse(Result(rc=0, out="", raw="", timed_out=True).ok)
        self.assertFalse(Result(rc=2, out="", raw="").ok)


if __name__ == "__main__":
    unittest.main()

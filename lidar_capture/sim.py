"""假 3588：在 pty 上跑一个真 bash，配一套 PATH 里的假命令。

为什么这样做：界面、判定、状态机都和实机完全相同，只有“命令落到哪”
不同。于是拔线/插线、nohup 存活、pcap 覆盖这些行为都是真的。
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import shlex
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

from .protocol import Console, LinkDown, strip_terminal

BANNER = """\
[    0.000000] Booting Linux on physical CPU 0x0000000000 [0x412fd050]
[    0.000000] Linux version 5.10.66 (root@build) (aarch64-linux-gnu-gcc 10.3.0)
[    0.000000] Machine model: Rockchip RK3588 EVB
[    1.234567] rk805 0-0020: registered regulator
[    2.345678] eth0: ethernet@fe1c0000, addr 8a:2b:3c:4d:5e:6f
[    2.456789] eth1: ethernet@fe2c0000, addr 8a:2b:3c:4d:5e:70
[    3.567890] systemd[1]: Detected architecture arm64.
Welcome to Debian GNU/Linux 11 (bullseye)

root@rk3588:~#
"""


class SimLink:
    """模拟“电脑 <-> 3588 的那根 USB 线”。"""

    def __init__(self, root: Path, scenario: str = "normal", rate: float = 2_000_000):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.scenario = scenario
        self.rate = rate
        self.stub_dir = self.root / "stub"
        self._install_stubs()
        self.master: int | None = None
        self.proc: subprocess.Popen | None = None
        self.connected = False
        self.boot_text = ""
        self._lock = threading.Lock()

    # ---------------- 假命令 ----------------
    def _write_exec(self, path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def _install_stubs(self) -> None:
        self.stub_dir.mkdir(parents=True, exist_ok=True)
        python = sys.executable or "/usr/bin/python3"
        pkill_bin = "/usr/bin/pkill" if Path("/usr/bin/pkill").exists() else "/bin/pkill"
        sim_tcpdump = Path(__file__).with_name("sim_tcpdump.py").resolve()

        self._write_exec(self.stub_dir / "sudo", "#!/bin/sh\n# 假 sudo：直接执行参数里的命令\nexec \"$@\"\n")
        self._write_exec(self.stub_dir / "ll", "#!/bin/sh\n# 假 ll：等价于 ls -l\nexec ls -l --color=never \"$@\"\n")
        self._write_exec(
            self.stub_dir / "dmesg",
            "#!/bin/sh\n"
            "if [ \"$SIM_SCENARIO\" = \"dmesg_fail\" ]; then\n"
            "  echo 'dmesg: read kernel buffer failed: Operation not permitted' >&2\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
        )
        self._write_exec(
            self.stub_dir / "pkill",
            "#!/bin/sh\n"
            "if [ \"$SIM_SCENARIO\" = \"stuck\" ]; then\n"
            "  # 假装杀掉了，其实没杀：用来测“暂停失败”提示\n"
            "  exit 0\n"
            "fi\n"
            f"exec {pkill_bin} \"$@\"\n",
        )
        self._write_exec(
            self.stub_dir / "tcpdump",
            "#!/bin/sh\n# 假 tcpdump\n"
            f"exec {shlex.quote(python)} {shlex.quote(str(sim_tcpdump))} \"$@\"\n",
        )
        banner = self.root / "banner.txt"
        banner.write_text(BANNER, encoding="utf-8")
        rcfile = self.root / "sim.bashrc"
        rcfile.write_text(
            f"cat {shlex.quote(str(banner))}\n"
            "PS1='root@rk3588:~# '\n"
            "export PS1\n",
            encoding="utf-8",
        )
        self.rcfile = rcfile

    # ---------------- 插线 / 拔线 ----------------
    def connect(self) -> None:
        """插上线：起一个新的 bash（相当于 3588 上开了一个新 shell）。"""
        with self._lock:
            if self.connected:
                return
            master, slave = os.openpty()
            try:  # 宽一点，避免回声换行把标记切断
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 48, 200, 0, 0))
            except OSError:
                pass
            env = os.environ.copy()
            env.update(
                PATH=f"{self.stub_dir}:{env.get('PATH', '/usr/bin:/bin')}",
                PS1="root@rk3588:~# ",
                HOME=str(self.root),
                TERM="vt100",
                LC_ALL="C",
                SIM_RATE=str(self.rate),
                SIM_SCENARIO=self.scenario,
            )
            env.pop("PYTHONPATH", None)

            def _child_setup() -> None:
                try:
                    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
                except OSError:
                    pass

            self.proc = subprocess.Popen(
                ["/bin/bash", "--noprofile", "--rcfile", str(self.rcfile), "-i"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=str(self.root),
                env=env,
                preexec_fn=_child_setup,
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave)
            os.set_blocking(master, False)
            self.master = master
            self.connected = True

    def disconnect(self) -> None:
        """拔线：关掉 pty。bash 会挂掉，但 nohup 起的 tcpdump 活着。"""
        with self._lock:
            self.connected = False
            if self.master is not None:
                try:
                    os.close(self.master)
                except OSError:
                    pass
                self.master = None

    def close(self) -> None:
        """彻底收工（程序退出时调用）：拔线并杀掉远端的假 tcpdump。"""
        self.disconnect()
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self.proc = None
        self.kill_remote()

    def kill_remote(self) -> None:
        for cmd in (["pkill", "-x", "tcpdump"], ["pkill", "-f", "sim_tcpdump.py"]):
            try:
                subprocess.run(cmd, timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:  # noqa: BLE001
                pass

    def hard_reset(self) -> None:
        """从 0 重新来：清掉远端进程与文件，重新插线。"""
        self.disconnect()
        time.sleep(0.2)
        self.kill_remote()
        time.sleep(0.2)
        for name in ("lidar.pcap", "nohup.out"):
            path = self.root / name
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        self.connect()

    # ---------------- 读写 ----------------
    def write(self, text: str) -> None:
        if not self.connected or self.master is None:
            raise LinkDown("数据线已断开，命令发不出去")
        data = text.replace("\n", "\r").encode("utf-8", "replace")
        try:
            os.write(self.master, data)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                self.disconnect()
                raise LinkDown("数据线已断开") from exc
            raise

    def read(self, timeout: float = 0.15) -> str:
        if not self.connected or self.master is None:
            raise LinkDown("数据线已断开")
        try:
            ready, _, _ = select.select([self.master], [], [], timeout)
        except OSError as exc:
            self.disconnect()
            raise LinkDown("数据线已断开") from exc
        if not ready:
            return ""
        try:
            data = os.read(self.master, 65536)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                self.disconnect()
                raise LinkDown("数据线已断开") from exc
            raise
        if not data:
            self.disconnect()
            raise LinkDown("数据线已断开")
        text = strip_terminal(data.decode("utf-8", "replace"))
        self.boot_text += text
        return text


class SimConsole(Console):
    name = "sim"

    def __init__(self, link: SimLink):
        self.link = link

    def open(self) -> None:
        self.link.connect()

    def close(self) -> None:
        self.link.disconnect()

    def write(self, text: str) -> None:
        self.link.write(text)

    def read(self, timeout: float) -> str:
        return self.link.read(timeout)

    def alive(self) -> bool:
        return self.link.connected

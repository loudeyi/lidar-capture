"""实机传输层：两种串口后端，界面与判定完全不感知差别。

minicom（默认）：真的拉起 minicom，命令走它的键盘转发路径，
  与你手敲 minicom 时看到的行为一致；程序通过一个 pty 与它对话。
native（兜底）：程序自己用 termios 直连串口，不依赖 minicom。
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import threading
import time
from pathlib import Path

from .protocol import Console, LinkDown, strip_terminal


class LinkError(Exception):
    """打不开链路（设备不存在、没权限……），带一句人话。"""


def _baud_const(baud: int) -> int:
    name = f"B{int(baud)}"
    if not hasattr(termios, name):
        raise LinkError(f"系统不支持 {baud} 波特率")
    return getattr(termios, name)


class NativeConsole(Console):
    name = "native"

    def __init__(self, device: str, baud: int):
        self.device = device
        self.baud = baud
        self.fd: int | None = None

    def open(self) -> None:
        try:
            fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            raise LinkError(f"串口设备不存在：{self.device}（检查 USB 线）") from exc
        except PermissionError as exc:
            raise LinkError(f"没有权限打开 {self.device}（把用户加进 dialout 组）") from exc
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0  # iflag: raw
        attrs[1] = 0  # oflag: raw
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = _baud_const(self.baud)
        attrs[5] = _baud_const(self.baud)
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        self.fd = fd

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def write(self, text: str) -> None:
        if self.fd is None:
            raise LinkDown("串口未打开")
        try:
            os.write(self.fd, text.replace("\n", "\r").encode("utf-8", "replace"))
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.ENODEV, errno.EBADF):
                raise LinkDown("串口已断开") from exc
            raise

    def read(self, timeout: float) -> str:
        if self.fd is None:
            raise LinkDown("串口未打开")
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
        except OSError as exc:
            raise LinkDown("串口已断开") from exc
        if not ready:
            return ""
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return ""
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.ENODEV, errno.EBADF):
                raise LinkDown("串口已断开") from exc
            raise
        return strip_terminal(data.decode("utf-8", "replace"))

    def alive(self) -> bool:
        return self.fd is not None


class MinicomConsole(Console):
    """把 minicom 当成“串口驱动”来用。

    我们持有一个 pty 的 master 端，minicom 的 stdin/stdout 挂在这个 pty 的
    slave 上，再由 minicom 自己用 -D 打开真串口。于是：写入 master 的字符
    == 你在 minicom 窗口里敲的键；从 master 读到的 == minicom 窗口的内容。
    """

    name = "minicom"

    def __init__(self, device: str, baud: int, workdir: Path | None = None):
        self.device = device
        self.baud = baud
        # 工作目录只用来放 minicom 的临时 HOME；没给就自己开一个
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="lidar-minicom-"))
        self.master: int | None = None
        self.proc: subprocess.Popen | None = None
        self._home: str | None = None

    def open(self) -> None:
        if shutil.which("minicom") is None:
            raise LinkError("系统里没有 minicom，请在配置里把 console 改成 native")
        if not Path(self.device).exists():
            raise LinkError(f"串口设备不存在：{self.device}（检查 USB 线）")
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._home = tempfile.mkdtemp(prefix="lidar-minicom-", dir=str(self.workdir))
        master, slave = os.openpty()
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 48, 200, 0, 0))
        except OSError:
            pass
        env = os.environ.copy()
        env.update(TERM="vt100", HOME=self._home, LC_ALL="C")

        def _child_setup() -> None:
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        self.proc = subprocess.Popen(
            ["minicom", "-o", "-8", "-D", self.device, "-b", str(self.baud)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            preexec_fn=_child_setup,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        os.set_blocking(master, False)
        self.master = master
        time.sleep(0.35)

    def close(self) -> None:
        if self.master is not None:
            try:  # Ctrl-A X 让 minicom 自己退出
                os.write(self.master, b"\x01x")
            except OSError:
                pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None

    def write(self, text: str) -> None:
        if self.master is None:
            raise LinkDown("minicom 未运行")
        try:
            os.write(self.master, text.replace("\n", "\r").encode("utf-8", "replace"))
        except OSError as exc:
            raise LinkDown("minicom 已退出") from exc

    def read(self, timeout: float) -> str:
        if self.master is None:
            raise LinkDown("minicom 未运行")
        try:
            ready, _, _ = select.select([self.master], [], [], timeout)
        except OSError as exc:
            raise LinkDown("minicom 已退出") from exc
        if ready:
            try:
                data = os.read(self.master, 65536)
            except BlockingIOError:
                data = b""
            except OSError as exc:
                raise LinkDown("minicom 已退出") from exc
            if data:
                return strip_terminal(data.decode("utf-8", "replace"))
        if self.proc is not None and self.proc.poll() is not None:
            raise LinkDown(f"minicom 已退出（返回码 {self.proc.returncode}）")
        return ""

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class SerialLink:
    """实机链路：按配置选择后端，负责插线/拔线的开关。"""

    name = "serial"

    def __init__(self, cfg, runtime_dir: Path | None = None):
        self.cfg = cfg
        self.runtime_dir = Path(runtime_dir) if runtime_dir else None
        self.console: Console | None = None

    def connect(self) -> Console:
        self.disconnect()
        if self.cfg.console == "minicom":
            console: Console = MinicomConsole(self.cfg.device, int(self.cfg.baud), self.runtime_dir)
        else:
            console = NativeConsole(self.cfg.device, int(self.cfg.baud))
        console.open()
        self.console = console
        return console

    def disconnect(self) -> None:
        if self.console is not None:
            try:
                self.console.close()
            finally:
                self.console = None

    def close(self) -> None:
        self.disconnect()

    def describe(self) -> str:
        return f"{self.cfg.console} @ {self.cfg.device} · {int(self.cfg.baud)} bps"


class SimLinkAdapter:
    """把 SimLink 包成与 SerialLink 一样的接口。"""

    name = "sim"

    def __init__(self, sim):
        self.sim = sim
        self.console: Console | None = None

    def connect(self) -> Console:
        self.sim.connect()
        from .sim import SimConsole

        self.console = SimConsole(self.sim)
        return self.console

    def disconnect(self) -> None:
        self.sim.disconnect()
        self.console = None

    def close(self) -> None:
        self.sim.close()
        self.console = None

    def describe(self) -> str:
        return f"模拟 3588 · 场景 {self.sim.scenario}"


def build_link(cfg, runtime_dir: Path | None = None):
    if cfg.is_sim:
        from .sim import SimLink

        sim = SimLink(Path(runtime_dir), scenario=cfg.sim_scenario, rate=float(cfg.sim_rate))
        return SimLinkAdapter(sim)
    return SerialLink(cfg, runtime_dir)

"""串口协议层：把“在 3588 上敲一条命令、把输出读回来”封装成一个函数调用。

发送格式（三行，逐条执行，避免 & 与 ; 打架）：

    echo __B<id>__
    <cmd>
    __rc=$?; echo __RC<id>__ $__rc; echo __E<id>__

第 1 行打出开始标记，第 2 行是真正的命令（`nohup ... &` 这类结尾是 & 的
命令也能原样执行），第 3 行记录上一条命令的返回码并打出结束标记。
stderr 和 stdout 都在同一个终端里，不需要重定向也能全收到。
解析时只认“整行就是标记”的行，取最后一对标记之间的内容，因此回显、
提示符、内核日志、minicom 重绘都不会干扰。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

# ---------- 终端输出清洗 ----------
_RE_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RE_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_RE_ESC = re.compile(r"\x1b[()#][0-9A-Za-z]|\x1b[=>]|\x1b\[[0-9;]*$")


def strip_terminal(text: str) -> str:
    """去掉 ANSI 控制序列、NUL、退格，统一换行。"""
    text = _RE_OSC.sub("", text)
    text = _RE_CSI.sub("", text)
    text = _RE_ESC.sub("", text)
    text = text.replace("\x00", "").replace("\x07", "")
    # 退格：删掉前一个字符
    while "\x08" in text:
        text = re.sub(r"[^\n]\x08", "", text, count=1)
        text = text.replace("\x08", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


class LinkDown(Exception):
    """串口断开（拔线、设备消失、minicom 退出）。"""


class ConsoleTimeout(Exception):
    pass


# ---------- 控制台接口 ----------
class Console:
    """字节通道。实现类只需要提供 open/close/write/read/alive。"""

    name = "console"

    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, text: str) -> None: ...
    def read(self, timeout: float) -> str: ...   # 返回已清洗的文本

    def alive(self) -> bool:
        return True


# ---------- 命令封装 ----------
@dataclass
class Result:
    rc: int | None
    out: str
    raw: str
    timed_out: bool = False
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return (not self.timed_out) and self.rc == 0

    def __str__(self) -> str:  # 便于日志
        return f"<rc={self.rc} out={self.out!r}>"


class RemoteShell:
    """在一个 Console 之上执行命令，并持续把输出喂给界面。"""

    def __init__(self, console: Console, on_text=None, on_link_down=None):
        self.console = console
        self.on_text = on_text
        self.on_link_down = on_link_down
        self._buf = ""
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._ident = 0
        self._reader: threading.Thread | None = None
        self._running = False
        self._link_down = False
        self.last_error: str | None = None
        self.last_cmd: str = ""

    # ---------- 生命周期 ----------
    def start(self) -> None:
        self._running = True
        self._link_down = False
        self._buf = ""
        self._reader = threading.Thread(target=self._read_loop, name="console-reader", daemon=True)
        self._reader.start()

    def stop(self) -> None:
        self._running = False
        with self._cv:
            self._cv.notify_all()

    # ---------- 读线程 ----------
    def _read_loop(self) -> None:
        while self._running:
            try:
                chunk = self.console.read(0.15)
            except LinkDown as exc:
                self._mark_link_down(str(exc) or "串口已断开")
                return
            except Exception as exc:  # noqa: BLE001 - 任何异常都视为链路问题
                self._mark_link_down(f"{type(exc).__name__}: {exc}")
                return
            if not chunk:
                continue
            if self.on_text:
                try:
                    self.on_text(chunk)
                except Exception:  # noqa: BLE001 - 界面回调不应影响读线程
                    pass
            with self._cv:
                self._buf = (self._buf + chunk)[-200_000:]
                self._cv.notify_all()

    def _mark_link_down(self, reason: str) -> None:
        with self._cv:
            self._link_down = True
            self.last_error = reason
            self._cv.notify_all()
        if self.on_link_down:
            try:
                self.on_link_down(reason)
            except Exception:  # noqa: BLE001
                pass

    @property
    def link_down(self) -> bool:
        return self._link_down

    # ---------- 命令 ----------
    def text(self) -> str:
        with self._lock:
            return self._buf

    def drain(self, seconds: float) -> str:
        """读若干秒内到达的所有内容（用于连接后的开机日志）。"""
        end = time.monotonic() + seconds
        with self._lock:
            start_len = len(self._buf)
        while time.monotonic() < end:
            if self._link_down:
                raise LinkDown(self.last_error or "串口已断开")
            time.sleep(0.05)
        with self._lock:
            return self._buf[start_len:]

    def wait_ready(self, timeout: float = 10.0, probes: int = 3) -> bool:
        """发 echo 探针，等到 3588 的 shell 真的回话。

        只认“单独一行 __PING__”，因此提示符、登录提示、回显都不会误判。
        """
        per = max(1.0, timeout / max(1, probes))
        for _ in range(probes):
            self.write_raw("echo __PING__\n")
            deadline = time.monotonic() + per
            target = "__PING__"
            while time.monotonic() < deadline:
                if self._link_down:
                    return False
                with self._lock:
                    buf = self._buf
                if any(line.strip() == target for line in buf.splitlines()):
                    return True
                time.sleep(0.05)
        return False

    def write_raw(self, text: str) -> None:
        if self._link_down:
            raise LinkDown(self.last_error or "串口已断开")
        self.console.write(text)

    def run(self, cmd: str, timeout: float = 10.0) -> Result:
        """执行一条命令并等待结束标记。超时返回 timed_out=True 的结果。"""
        with self._lock:
            self._ident += 1
            self.last_cmd = cmd.strip()
            ident = self._ident
        wrapped = (
            f"echo __B{ident}__\n{cmd}\n"
            f"__rc=$?; echo __RC{ident}__ $__rc; echo __E{ident}__"
        )
        mark_begin = f"__B{ident}__"
        mark_end = f"__E{ident}__"
        t0 = time.monotonic()
        self.write_raw(wrapped + "\n")
        deadline = t0 + timeout
        while True:
            if self._link_down:
                raise LinkDown(self.last_error or "串口已断开")
            with self._lock:
                buf = self._buf
            parsed = _parse_marked(buf, mark_begin, mark_end, ident, cmd)
            if parsed is not None:
                out, rc = parsed
                return Result(rc=rc, out=out, raw=buf[-4000:], elapsed=time.monotonic() - t0)
            if time.monotonic() >= deadline:
                return Result(rc=None, out="", raw=buf[-4000:], timed_out=True,
                              elapsed=time.monotonic() - t0)
            with self._cv:
                self._cv.wait(0.05)

    def send_line(self, text: str) -> None:
        """手动模式：原样把一行送到 3588。"""
        self.write_raw(text + "\n")


def _is_token_line(stripped: str, token: str) -> bool:
    """整行就是 token，或者行首只是提示符（root@rk3588:~# __E1__）。"""
    if stripped == token:
        return True
    if stripped.endswith(token):
        prefix = stripped[: -len(token)].rstrip()
        if prefix and prefix[-1] in "#$>" and "echo " not in prefix:
            return True
    return False


def _line_spans(buf: str, token: str) -> list[tuple[int, int, bool]]:
    """找出“整行就是 token”的那些行，返回 (行首, 行尾, 是否已换行)。"""
    spans = []
    pos = 0
    for line in buf.splitlines(keepends=True):
        if _is_token_line(line.strip(), token):
            spans.append((pos, pos + len(line), line.endswith("\n")))
        pos += len(line)
    return spans


def _strip_echo(lines: list[str], ident: int, cmd: str | None) -> list[str]:
    """去掉我们自己那三行命令的回声，别混进命令输出里。"""
    out: list[str] = []
    tail = f"__RC{ident}__ $__rc; echo __E{ident}__"
    cmd_text = (cmd or "").strip()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.endswith(tail):
            continue
        if idx == 0 and cmd_text and stripped.endswith(cmd_text):
            continue
        out.append(line)
    return out


def _parse_marked(
    buf: str, mark_begin: str, mark_end: str, ident: int, cmd: str | None = None
) -> tuple[str, int | None] | None:
    """取“最后一个完整结束标记”之前、“最后一个开始标记”之后的内容。

    只认整行等于标记的行，因此：
      * 回声里那行长长的命令（整行 ≠ 标记）不会误判
      * 提示符、内核日志、minicom 重绘都不会干扰
    """
    ends = _line_spans(buf, mark_end)
    if not ends:
        return None
    end_start, _end_stop, end_complete = ends[-1]
    if not end_complete:
        return None  # 只收到了半个标记，继续等
    begins = [span for span in _line_spans(buf, mark_begin) if span[1] <= end_start]
    if not begins:
        return None
    begin_stop = begins[-1][1]
    content = buf[begin_stop:end_start]
    rc_match = re.search(rf"__RC{ident}__\s+(-?\d+)", content)
    rc = int(rc_match.group(1)) if rc_match else None
    lines = [ln for ln in content.splitlines() if not ln.strip().startswith(f"__RC{ident}__")]
    lines = _strip_echo(lines, ident, cmd)
    return "\n".join(lines).strip("\n"), rc

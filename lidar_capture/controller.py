"""状态机：把四步流程 + 轮询 + 停止确认编排起来。

界面只发“动作”，所有串口交互都在这个线程里串行执行，因此不需要给
命令加锁，也不会出现两条命令的标记互相干扰。
"""

from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque

from .config import Config
from .events import EventLog
from .monitor import SizeTrack, human_rate, human_size
from .protocol import LinkDown, RemoteShell

# 状态
IDLE = "idle"
CONNECTING = "connecting"
READY = "ready"
DMESG_OK = "dmesg_ok"
STARTING = "starting"
RECORDING = "recording"
STOPPING = "stopping"
STOPPED = "stopped"
STOP_FAILED = "stop_failed"
DISCONNECTED = "disconnected"
ERROR = "error"

STATE_TEXT = {
    IDLE: "待开始",
    CONNECTING: "正在连接 3588",
    READY: "已连接，等待关闭 dmesg 打印",
    DMESG_OK: "已就绪，可以开始录制",
    STARTING: "已发送录制命令，等待数据包",
    RECORDING: "正在录制",
    STOPPING: "正在停止录制",
    STOPPED: "录制已停止",
    STOP_FAILED: "停止失败",
    DISCONNECTED: "数据线已断开",
    ERROR: "出错了",
}

_STEP_TITLES = {
    "connect": "连接 3588",
    "dmesg": "关闭控制台打印",
    "start": "开始录制",
    "stop": "停止录制",
}

# ls -l / ll 的一行：-rw-r--r-- 1 root root 123456 Oct 28 14:20 lidar.pcap
_RE_LL = re.compile(r"^[-dlbcps][rwxsStT-]{9}[.+@]?\s+\d+\s+\S+\s+\S+\s+(\d+)\s", re.M)
_RE_BARE_SIZE = re.compile(r"^\s*(\d+)\s*$", re.M)
# 我们自己的三行协议在终端里的回声，别显示给用户
_RE_PROTOCOL_LINE = re.compile(
    r"^(?:.*[#$]\s*)?(?:echo\s+)?__B\d+__$"
    r"|^(?:.*[#$]\s*)?__rc=\$\?; echo __RC\d+__ \$__rc; echo __E\d+__$"
    r"|^(?:.*[#$]\s*)?__RC\d+__ -?\d+$"
    r"|^(?:.*[#$]\s*)?__E\d+__$"
    r"|^(?:.*[#$]\s*)?(?:echo\s+)?__PING__$"
)


def parse_size(text: str) -> int | None:
    """从 ll / ls -l 的输出里取出文件大小。"""
    if not text:
        return None
    match = _RE_LL.search(text)
    if match:
        return int(match.group(1))
    match = _RE_BARE_SIZE.search(text)
    if match:
        return int(match.group(1))
    return None


class Controller(threading.Thread):
    def __init__(self, cfg: Config, link, log: EventLog, emit):
        super().__init__(name="lidar-controller", daemon=True)
        self.cfg = cfg
        self.link = link
        self.log = log
        self.emit = emit
        self.q: queue.Queue = queue.Queue()

        self.state = IDLE
        self.link_ok = False
        self.shell: RemoteShell | None = None
        self.track = SizeTrack(window=max(4.0, cfg.poll_interval * 3), keep=400)
        self.session_start: float | None = None
        self.session_stop: float | None = None
        self.last_size: int | None = None
        self.proc_running: bool | None = None
        self.error: str | None = None
        self.stop_reason: str | None = None
        self.start_deadline: float | None = None
        self.start_warned = False
        self.steps: dict[str, dict] = {
            key: {"status": "pending", "detail": ""} for key in _STEP_TITLES
        }
        self._transcript: deque[str] = deque(maxlen=4000)
        self._running = True
        self._poll_due = 0.0
        self._proc_due = 0.0
        self._poll_count = 0
        self._last_elapsed_emit = 0.0
        self._noise_buf = ""
        self._quiet = 0
        self._last_verdict: str | None = None
        self._dmesg_done = False

    # ---------------- 对外动作 ----------------
    def connect(self) -> None:
        self.q.put(("connect", ()))

    def run_dmesg(self) -> None:
        self.q.put(("dmesg", ()))

    def start_recording(self) -> None:
        self.q.put(("start", ()))

    def stop_recording(self) -> None:
        self.q.put(("stop", ()))

    def reconnect(self) -> None:
        self.q.put(("reconnect", ()))

    def hard_reset(self) -> None:
        self.q.put(("reset", ()))

    def manual(self, text: str) -> None:
        self.q.put(("manual", (text,)))

    def shutdown(self) -> None:
        self.q.put(("shutdown", ()))

    # ---------------- 主循环 ----------------
    def run(self) -> None:
        while self._running:
            try:
                name, args = self.q.get(timeout=0.1)
            except queue.Empty:
                name, args = None, ()
            if name:
                try:
                    getattr(self, f"_do_{name}")(*args)
                except LinkDown as exc:
                    self._handle_link_down(str(exc) or "数据线已断开")
                except Exception as exc:  # noqa: BLE001 - 任何意外都要显示出来
                    self.error = f"{type(exc).__name__}: {exc}"
                    self._set_state(ERROR, self.error)
                    self._toast("error", self.error)
            self._tick()
        self._teardown_shell()
        try:
            self.link.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 基础动作 ----------------
    def _do_connect(self) -> None:
        self._dmesg_done = False
        self._set_state(CONNECTING)
        self._step("connect", "running", "正在打开数据线…")
        if not self._open():
            return
        self._set_state(READY)
        self._step("connect", "ok", "3588 已回话，串口通道正常")
        self._toast("ok", "已连上 3588")
        if self.cfg.auto_connect:
            self._do_dmesg()

    def _do_reconnect(self) -> None:
        self._teardown_shell()
        self._set_state(CONNECTING)
        self._step("connect", "running", "正在重新连接…")
        if not self._open():
            return
        self._step("connect", "ok", "已重新连接")
        proc = self._query_proc()
        size = self._query_size()
        if size is not None:
            self.last_size = size
            self.track.clear()
            self.track.add(time.time(), size)
        if proc:
            self._set_state(RECORDING)
            self._toast("ok", "已重新连接：3588 仍在录制，继续监视")
            if self.session_start is None:
                self.session_start = time.time()
            self._step("start", "ok", "断线前已开始录制，远端仍在写 pcap")
            self._poll_due = 0.0
        else:
            self._set_state(DMESG_OK if self._dmesg_done else READY)
            self._toast("info", "已重新连接：3588 上没有 tcpdump 在跑")

    def _do_dmesg(self) -> None:
        if not self._ensure_link():
            return
        self._step("dmesg", "running", self.cfg.cmd_dmesg)
        result = self.shell.run(self.cfg.cmd_dmesg, timeout=10)
        self.log.event("dmesg", rc=result.rc, out=result.out[:400])
        if result.timed_out:
            self._step("dmesg", "fail", "命令超时，没有返回")
            self._toast("warn", "dmesg 超时（可能要输密码，或串口被占用）")
            return
        if result.rc == 0:
            self._dmesg_done = True
            self._step("dmesg", "ok", f"rc=0 · {self.cfg.cmd_dmesg}")
            if self.state in (READY, DISCONNECTED):
                self._set_state(DMESG_OK)
        else:
            detail = (result.out or "").strip().splitlines()
            self._step("dmesg", "fail", f"rc={result.rc} · {detail[-1] if detail else '无输出'}")
            self._toast("warn", "关闭内核打印失败，看看下方输出（可能需要密码）")

    def _do_start(self) -> None:
        if not self._ensure_link():
            return
        if self.state in (STARTING, RECORDING):
            self._toast("info", "已经在录制，先停掉再重新开始")
            self._do_stop(quiet=True)
            if self.state == STOP_FAILED:
                return
        if not self._dmesg_done:
            self._do_dmesg()
        if not self._dmesg_done:
            self._step("start", "fail", "上一步没成功，先解决关闭打印的问题")
            return

        # 新一轮录制：日志整体覆盖
        self.log.reset()
        self._flush_transcript_to_log()
        self.session_start = time.time()
        self.session_stop = None
        self.last_size = None
        self.proc_running = None
        self.stop_reason = None
        self.start_warned = False
        self.track.clear()
        self._poll_count = 0
        self._set_state(STARTING)
        self._step("start", "running", self.cfg.start_cmd())
        self.log.event("start", cmd=self.cfg.start_cmd())

        result = self.shell.run(self.cfg.start_cmd(), timeout=12)
        self.log.event("start_result", rc=result.rc, out=result.out[:400])
        if result.timed_out:
            self._step("start", "fail", "命令超时（nohup 没有返回）")
            self._set_state(ERROR, "启动录制超时")
            return
        if result.rc != 0:
            tail = (result.out or "").strip().splitlines()
            self._step("start", "fail", f"rc={result.rc} · {tail[-1] if tail else '无输出'}")
            self._set_state(ERROR, "启动录制失败")
            self._toast("error", "启动失败，看下方输出")
            return

        self.start_deadline = time.time() + self.cfg.start_timeout
        self._poll_due = 0.0
        self._toast("info", "命令已发出，正在确认 pcap 是否在变大…")
        self._poll_size(force=True)

    def _do_stop(self, quiet: bool = False) -> None:
        if not self._ensure_link():
            return
        self._set_state(STOPPING)
        self._step("stop", "running", self.cfg.cmd_stop)
        self.log.event("stop", cmd=self.cfg.cmd_stop)
        result = self.shell.run(self.cfg.cmd_stop, timeout=10)
        self.log.event("stop_result", rc=result.rc, out=result.out[:400])
        if result.timed_out:
            self._step("stop", "fail", "pkill 超时")
            self._set_state(STOP_FAILED, "停止命令超时")
            self.stop_reason = "stop 命令超时"
            return

        deadline = time.time() + self.cfg.stop_timeout
        attempt = 0
        ok = False
        reason = "未知"
        while time.time() < deadline:
            attempt += 1
            time.sleep(min(0.6, max(0.2, self.cfg.poll_interval * 0.4)))
            size = self._query_size()
            proc = self._query_proc()
            now = time.time()
            if size is not None:
                self.last_size = size
            self.track.add(now, size)
            self._emit(
                {
                    "type": "stopcheck",
                    "attempt": attempt,
                    "proc": proc,
                    "size": size,
                    "state": "stopping",
                    "detail": f"第 {attempt} 次确认：进程={'在跑' if proc else ('没了' if proc is False else '未知')}，"
                    f"pcap={human_size(size)}",
                }
            )
            stable = self.track.stable_for(now, max(1.2, self.cfg.poll_interval * 1.2))
            if proc is False and (size is None or stable):
                ok = True
                reason = "远端已无 tcpdump 进程，且 pcap 大小不再变化"
                break
            if proc is False and size is None:
                ok = True
                reason = "远端已无 tcpdump 进程，且 pcap 已不存在"
                break
            if proc is None and stable:
                ok = True
                reason = "pcap 大小已稳定（进程查询不可用）"
                break
            reason = "tcpdump 进程仍在运行" if proc else "pcap 大小仍在变化"
            self._emit({"type": "size", "size": size, "verdict": self.track.verdict(now)})

        self.proc_running = self._query_proc() if not ok else False
        self._emit({"type": "proc", "running": bool(self.proc_running)})
        if ok:
            self.session_stop = time.time()
            self._set_state(STOPPED)
            if self.session_start:
                self._emit({"type": "elapsed", "seconds": self.session_stop - self.session_start})
            self._emit(
                {"type": "size", "size": self.last_size, "verdict": "stalled", "rate": 0.0}
            )
            self._step("stop", "ok", reason)
            self._toast("ok", "已停止录制并确认（pcap 不再增长）")
            self.log.event("stopped", reason=reason, attempts=attempt)
            self._write_summary(ok=True, reason=reason)
        else:
            self._set_state(STOP_FAILED, reason)
            self._step("stop", "fail", f"{reason}（试了 {attempt} 次）")
            self.log.event("stop_failed", reason=reason, attempts=attempt)
            if not quiet:
                self._toast("error", f"停止失败：{reason}")
            self._write_summary(ok=False, reason=reason)

    def _do_reset(self) -> None:
        self._teardown_shell()
        self.log.event("reset")
        self._toast("info", "正在从 0 重新开始…")
        try:
            if self.cfg.is_sim:
                self.link.sim.hard_reset()
            else:
                self.link.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.session_start = None
        self._last_verdict = None
        self.track.clear()
        self._last_verdict = None
        self._dmesg_done = False
        for key in self.steps:
            self.steps[key] = {"status": "pending", "detail": ""}
        self._set_state(IDLE)
        self._do_connect()

    def _do_manual(self, text: str) -> None:
        if not self._ensure_link():
            return
        self.log.event("manual", cmd=text)
        result = self.shell.run(text, timeout=12)
        self._emit({"type": "manual", "cmd": text, "out": result.out, "rc": result.rc})

    def _do_shutdown(self) -> None:
        self._running = False

    # ---------------- 链路 ----------------
    def _open(self) -> bool:
        try:
            console = self.link.connect()
        except Exception as exc:  # noqa: BLE001 - LinkError 或 OSError 都是“连不上”
            msg = str(exc) or f"{type(exc).__name__}"
            self.error = msg
            self.link_ok = False
            self._emit({"type": "link", "connected": False, "detail": msg})
            self._step("connect", "fail", msg)
            self._set_state(ERROR, msg)
            self._toast("error", msg)
            return False

        self.shell = RemoteShell(console, on_text=self._on_text, on_link_down=self._on_link_down)
        self.shell.start()
        self.link_ok = True
        self._emit({"type": "link", "connected": True, "detail": self.link.describe()})
        self.log.event("link_open", backend=self.link.describe())

        if not self.shell.wait_ready(timeout=self.cfg.connect_timeout):
            self.error = "3588 没有回话"
            self._step("connect", "fail", "串口开了，但 3588 没有回话（可能没启动完/被别的程序占用）")
            self._emit({"type": "link", "connected": False, "detail": self.error})
            self._set_state(ERROR, self.error)
            self._toast("error", self.error)
            self._teardown_shell()
            return False
        return True

    def _ensure_link(self) -> bool:
        if self.shell is not None and self.link_ok:
            return True
        self._toast("warn", "链路不通，正在尝试重新连接…")
        return self._open()

    def _teardown_shell(self) -> None:
        self.link_ok = False
        if self.shell is not None:
            self.shell.stop()
            self.shell = None
        try:
            self.link.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def _handle_link_down(self, reason: str) -> None:
        self.link_ok = False
        self.shell = None
        self._emit({"type": "link", "connected": False, "detail": reason})
        if self.state in (CONNECTING, ERROR):
            return
        if self.state in (STARTING, RECORDING, STOPPING, DMESG_OK, READY, STOPPED):
            self._set_state(DISCONNECTED, reason)
            self._toast(
                "warn",
                "数据线断开——3588 还在按最后一条命令继续，插回线后点“重新连接”",
            )
        self.log.event("link_down", reason=reason)

    def _on_link_down(self, reason: str) -> None:
        self._handle_link_down(reason)

    def _on_text(self, text: str) -> None:
        self._noise_buf += text
        lines = self._noise_buf.split("\n")
        self._noise_buf = lines.pop()
        for line in lines:
            self._publish_line(line)
        if len(self._noise_buf) > 4000:
            self._publish_line(self._noise_buf)
            self._noise_buf = ""

    def _publish_line(self, line: str, tag: str | None = None) -> None:
        """输出控制台只在“有内容”的行上刷新，协议自己的回声不显示。"""
        if self._quiet:
            return
        if self._is_protocol_noise(line):
            return
        text = line + "\n"
        self._transcript.append(text)
        self.log.console(text)
        self._emit({"type": "text", "text": text, "tag": tag})

    def _is_protocol_noise(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return True
        if _RE_PROTOCOL_LINE.match(stripped):
            return True
        last = getattr(self.shell, "last_cmd", "") if self.shell else ""
        return bool(last) and stripped.endswith(last)

    def _flush_transcript_to_log(self) -> None:
        head = "".join(self._transcript)[-40000:]
        if head:
            self.log.console("\n==== 连接阶段记录（本次录制前） ====\n" + head + "\n")

    # ---------------- 轮询 ----------------
    def _tick(self) -> None:
        if not self._running:
            return
        now = time.time()
        if self.session_start and self.state in (STARTING, RECORDING, STOPPING, STOP_FAILED):
            if now - self._last_elapsed_emit >= 1.0:
                self._last_elapsed_emit = now
                end = self.session_stop or now
                self._emit({"type": "elapsed", "seconds": max(0.0, end - self.session_start)})

        if not self.link_ok:
            return
        if self.state not in (STARTING, RECORDING, DMESG_OK, READY, STOP_FAILED):
            return
        if now < self._poll_due:
            return
        self._poll_due = now + self.cfg.poll_interval
        self._poll_size()
        if now >= self._proc_due:
            self._proc_due = now + self.cfg.poll_interval * 3
            self.proc_running = self._query_proc()
            self._emit({"type": "proc", "running": self.proc_running})

        if self.state == STARTING:
            if self.track.is_growing(time.time()):
                self._set_state(RECORDING)
                self._step("start", "ok", "pcap 正在变大：已经在录了，可以拔线起飞")
                self._toast("ok", "开始录制成功（pcap 持续增大）")
                self.log.event("recording", size=self.last_size)
            elif self.start_deadline and time.time() > self.start_deadline and not self.start_warned:
                self.start_warned = True
                self._step("start", "warn", "命令已发出，但 pcap 一直没变大")
                self._toast(
                    "warn",
                    "pcap 大小还没变化——tcpdump 可能没起来，或还没开始收包",
                )

    def _poll_size(self, force: bool = False) -> int | None:
        size = self._query_size()
        now = time.time()
        if size is not None:
            self.last_size = size
        self.track.add(now, size)
        verdict = self.track.verdict(now)
        self._note_size_change(verdict, size, now)
        self._emit(
            {
                "type": "size",
                "size": size,
                "verdict": verdict,
                "rate": self.track.rate(now),
                "proc": self.proc_running,
            }
        )
        return size

    def _query_size(self) -> int | None:
        if not self.shell or not self.link_ok:
            return None
        self._quiet += 1
        try:
            result = self.shell.run(self.cfg.cmd_prefix() + self.cfg.size_cmd(), timeout=6)
        finally:
            self._quiet -= 1
        if result.timed_out:
            return None
        if result.rc != 0:
            return None
        return parse_size(result.out)

    def _query_proc(self) -> bool | None:
        if not self.shell or not self.link_ok:
            return None
        self._quiet += 1
        try:
            result = self.shell.run(self.cfg.cmd_proc, timeout=5)
        finally:
            self._quiet -= 1
        if result.timed_out or result.rc is None:
            return None
        return result.rc == 0

    def _note_size_change(self, verdict: str, size: int | None, now: float) -> None:
        """只在“结论变了”的时候往控制台写一行 ll 证据，免得轮询刷屏。"""
        if verdict == self._last_verdict:
            return
        if len(self.track) < 3:
            return
        # 刚开始采样时“还没长起来”不等于停住了，得真稳住 2 秒才这么写
        if verdict in ("stalled", "empty") and not self.track.stable_for(now, 2.0):
            return
        self._last_verdict = verdict
        label = {
            "growing": "在变长",
            "stalled": "不再变化",
            "empty": "还是空文件",
            "missing": "还没生成",
            "shrunk": "变小了",
        }.get(verdict)
        if not label:
            return
        shown = human_size(size) if size is not None else "—"
        self._publish_line(f"ll {self.cfg.remote_file} → {shown}（{label}）", tag="dim")

    # ---------------- 状态与通知 ----------------
    def _emit(self, payload: dict) -> None:
        try:
            self.emit(payload)
        except Exception:  # noqa: BLE001 - 界面没了也不能让状态机死掉
            pass

    def _set_state(self, state: str, detail: str = "") -> None:
        if state == self.state and not detail:
            return
        self.state = state
        self._emit({"type": "state", "state": state, "text": STATE_TEXT.get(state, state), "detail": detail})

    def _step(self, key: str, status: str, detail: str = "") -> None:
        self.steps[key] = {"status": status, "detail": detail}
        self._emit({"type": "step", "key": key, "status": status, "detail": detail})

    def _toast(self, level: str, text: str) -> None:
        self._emit({"type": "toast", "level": level, "text": text})
        self.log.event("toast", level=level, text=text)

    def _write_summary(self, ok: bool, reason: str) -> None:
        duration = None
        if self.session_start:
            duration = (self.session_stop or time.time()) - self.session_start
        self.log.summary(
            **{
                "结果": "已停止" if ok else "停止失败",
                "原因": reason,
                "模式": "模拟" if self.cfg.is_sim else "实机",
                "链路": self.link.describe(),
                "开始时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.session_start or time.time())),
                "录制时长": f"{duration:.0f} 秒" if duration else "—",
                "pcap 大小": human_size(self.last_size),
                "平均速率": human_rate(self.track.rate(time.time())),
                "日志目录": str(self.log.dir),
            }
        )

    # ---------------- 调试 ----------------
    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "link_ok": self.link_ok,
            "size": self.last_size,
            "proc": self.proc_running,
            "steps": {k: dict(v) for k, v in self.steps.items()},
        }

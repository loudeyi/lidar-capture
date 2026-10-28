"""会话日志：每次开始录制时整体覆盖，文件名固定，不堆积。

写到 <项目>/logs/ 下：
  session.log   人读的时间线（含串口原始输出）
  events.jsonl  机读事件
  summary.md    停止时生成的一段摘要
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

LOG_FILES = ("session.log", "events.jsonl", "summary.md")


class EventLog:
    def __init__(self, log_dir: Path):
        self.dir = Path(log_dir)
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def reset(self) -> None:
        """开始新一轮录制：清掉上一次的全部日志（与 pcap 覆盖语义一致）。"""
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.dir.glob("*")):
                if path.is_file() and (path.suffix in (".log", ".jsonl", ".md") or path.name in LOG_FILES):
                    path.unlink()

    def session_path(self) -> Path:
        return self.dir / "session.log"

    # ---------- 写入 ----------
    def _append(self, name: str, text: str) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / name, "a", encoding="utf-8") as fh:
                fh.write(text)

    def console(self, text: str) -> None:
        """串口原始输出（已去 ANSI），原样落到 session.log。"""
        if text:
            self._append("session.log", text)

    def note(self, text: str) -> None:
        self.event("note", text=text)

    def event(self, kind: str, **fields) -> None:
        now = time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(now))
        rec = {"ts": round(now, 3), "time": stamp, "event": kind, **fields}
        self._append("events.jsonl", json.dumps(rec, ensure_ascii=False) + "\n")
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        self._append("session.log", f"\n[{stamp}] <{kind}> {detail}\n")

    def summary(self, **fields) -> None:
        lines = ["# LiDAR 采集摘要", ""]
        for key, value in fields.items():
            lines.append(f"- **{key}**：{value}")
        lines.append("")
        self._append("summary.md", "\n".join(lines))

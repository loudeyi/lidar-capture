"""大小序列 → 结论。

这一层是纯逻辑：不碰串口、不碰界面、不读时钟。
核心要回答三个问题：
  1) 文件是不是在变大（= 真的在录）
  2) 是不是已经停稳了（= 停止成功）
  3) 现在大概多快（MB/s，给人看）
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

MB = 1024 * 1024


@dataclass(frozen=True)
class Sample:
    t: float
    size: int | None


class SizeTrack:
    """保存最近一段采样，给出“在长 / 停滞 / 消失 / 变小”的判定。"""

    def __init__(self, window: float = 8.0, keep: int = 240):
        self.window = window
        self._samples: deque[Sample] = deque(maxlen=keep)

    # ---------- 写入 ----------
    def add(self, t: float, size: int | None) -> None:
        self._samples.append(Sample(t, size))

    def clear(self) -> None:
        self._samples.clear()

    def __len__(self) -> int:
        return len(self._samples)

    @property
    def samples(self) -> list[Sample]:
        return list(self._samples)

    def last(self) -> Sample | None:
        return self._samples[-1] if self._samples else None

    def last_known_size(self) -> int | None:
        for s in reversed(self._samples):
            if s.size is not None:
                return s.size
        return None

    def _window(self, now: float) -> list[Sample]:
        return [s for s in self._samples if now - s.t <= self.window]

    # ---------- 判定 ----------
    def verdict(self, now: float) -> str:
        """growing / stalled / missing / shrunk / empty"""
        window = self._window(now)
        if not window:
            return "empty"
        last = window[-1]
        if last.size is None:
            return "missing"
        prev = [s.size for s in window[:-1] if s.size is not None]
        if prev and max(prev) > last.size:
            return "shrunk"
        # 需要“至少 3 个采样、净增为正、且最后一段仍在增”才算在长
        increasing = 0
        for a, b in zip(window, window[1:]):
            if a.size is not None and b.size is not None and b.size > a.size:
                increasing += 1
        if increasing >= 2 and last.size > (prev[0] if prev else 0):
            return "growing"
        return "stalled"

    def is_growing(self, now: float) -> bool:
        return self.verdict(now) == "growing"

    def stable_for(self, now: float, seconds: float) -> bool:
        """最近 seconds 秒内大小是否一直没变（用于确认“已停止”）。"""
        window = [s for s in self._samples if now - s.t <= seconds]
        if len(window) < 2:
            return False
        sizes = {s.size for s in window}
        if len(sizes) != 1:
            return False
        # 窗口必须覆盖足够长的时间，避免刚开始采样就误判
        return (window[-1].t - window[0].t) >= seconds * 0.7

    def rate(self, now: float) -> float | None:
        """最近窗口内的平均速率 B/s。"""
        window = [s for s in self._window(now) if s.size is not None]
        if len(window) < 2:
            return None
        span = window[-1].t - window[0].t
        if span <= 0:
            return None
        return (window[-1].size - window[0].size) / span


# ---------- 展示格式 ----------
def human_size(size: int | None) -> str:
    if size is None:
        return "—"
    v = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(v)} B"
            return f"{v:.2f} {unit}"
        v /= 1024
    return f"{v:.2f} TB"


def human_rate(rate: float | None) -> str:
    if rate is None:
        return "—"
    return human_size(int(rate)) + "/s"


def human_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

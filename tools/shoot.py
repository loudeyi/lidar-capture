#!/usr/bin/env python3
"""把界面跑起来并连拍五张图（连接 / 录制 / 拔线 / 重连 / 停止），用来检查视觉效果。

    python3 tools/shoot.py docs/shots
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import ImageGrab  # noqa: E402

from lidar_capture.config import Config, ensure_dirs  # noqa: E402
from lidar_capture.ui import App  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/shots").resolve()
OUT.mkdir(parents=True, exist_ok=True)
WORK = Path(tempfile.mkdtemp(prefix="lidar-shots-"))

cfg = Config(
    profile="sim",
    poll_interval=0.6,
    sim_rate=1_200_000,
    connect_timeout=10.0,
    start_timeout=10.0,
    stop_timeout=6.0,
    project_dir=WORK,
    sim_root=str(WORK / "runtime"),
)
ensure_dirs(cfg)

app = App(cfg)
app.geometry("1640x1080+10+10")
app.update()


def grab(name: str) -> None:
    for _ in range(3):
        app.update_idletasks()
        app.update()
    x, y = app.winfo_rootx(), app.winfo_rooty()
    w, h = app.winfo_width(), app.winfo_height()
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), xdisplay=":0")
    path = OUT / name
    img.save(path)
    print("saved", path, img.size, flush=True)


def unplug() -> None:
    app.link.sim.disconnect()


def replug() -> None:
    app.link.sim.connect()
    app.ctrl.reconnect()


steps = [
    (0.6, lambda: app.ctrl.connect()),
    (4.0, lambda: grab("01-connected.png")),
    (4.4, lambda: app.ctrl.start_recording()),
    (11.0, lambda: grab("02-recording.png")),
    (11.4, unplug),
    (13.4, lambda: grab("03-disconnected.png")),
    (13.8, replug),
    (17.4, lambda: grab("04-reconnected.png")),
    (17.8, lambda: app.ctrl.stop_recording()),
    (22.6, lambda: grab("05-stopped.png")),
    (23.2, lambda: app._on_close()),
]

for delay, action in steps:
    app.after(int(delay * 1000), action)

app.mainloop()
print("done")

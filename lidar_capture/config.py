"""配置：默认值与 config.toml 的读写。

所有现场可调的参数都集中在这里，界面不需要再接设置页——
改 config.toml 重启即可（界面底部有“打开配置文件”按钮）。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"

DEFAULT_TOML = """\
# LiDAR 采集控制台配置
# 改完重启程序生效。

# sim = 本机模拟（假 3588，用于演示与自测）；real = 现场实机
profile = "sim"

# ---- 实机参数 ----
device     = "/dev/ttyUSB0"   # 串口设备（USB 转 TypeC 到 3588）
baud       = 1500000
interface  = "eth1"           # 3588 上抓包的网口
remote_file = "lidar.pcap"    # 3588 上的文件名（固定名，重新录制自动覆盖）
remote_dir  = ""              # 登录后的默认目录；留空表示就是默认目录

# ---- 串口后端 ----
# minicom = 真的拉起 minicom（与你现在手敲的一致，推荐）
# native  = 程序自己直连串口（没有 minicom 时兜底）
console = "minicom"

# ---- 行为 ----
auto_connect   = true         # 启动后自动执行第一步
poll_interval  = 2.0          # 每隔几秒向 3588 询问一次文件大小
connect_timeout = 12.0        # 等 3588 回话的超时（秒）
start_timeout  = 30.0         # 发出录制命令后，等 pcap 开始变大的最长时间
stop_timeout   = 12.0         # 发出停止命令后，确认真的停住的最长时间

# ---- 命令模板（{iface} {file} 会被替换）----
cmd_dmesg = "sudo dmesg -D"
cmd_start = "nohup tcpdump -i {iface} -w {file} &"
cmd_stop  = "sudo pkill tcpdump"
cmd_size  = "ll {file}"
cmd_proc  = "pgrep -x tcpdump"
"""


def _bool(v) -> bool:
    return bool(v)


@dataclass
class Config:
    profile: str = "sim"
    device: str = "/dev/ttyUSB0"
    baud: int = 1_500_000
    interface: str = "eth1"
    remote_file: str = "lidar.pcap"
    remote_dir: str = ""
    console: str = "minicom"
    auto_connect: bool = True
    poll_interval: float = 2.0
    connect_timeout: float = 12.0
    start_timeout: float = 30.0
    stop_timeout: float = 12.0
    log_dir: str = "logs"
    cmd_dmesg: str = "sudo dmesg -D"
    cmd_start: str = "nohup tcpdump -i {iface} -w {file} &"
    cmd_stop: str = "sudo pkill tcpdump"
    cmd_size: str = "ll {file}"
    cmd_proc: str = "pgrep -x tcpdump"
    # 模拟器参数
    sim_rate: float = 2_000_000.0     # 假 tcpdump 的写入速率 B/s
    sim_scenario: str = "normal"
    sim_delay: float = 6.0            # stall_then_grow 场景的等待秒数
    sim_root: str = ""                # 留空 = <项目>/runtime

    # 非配置文件字段
    project_dir: Path = field(default_factory=lambda: PROJECT_ROOT)

    def __post_init__(self) -> None:
        # 假的 3588 必须待在 runtime/ 里，别把 pcap、nohup.out、stub 写进项目根目录
        if not self.sim_root:
            self.sim_root = str(self.project_dir / "runtime")

    # ---------- 派生 ----------
    @property
    def logs_dir(self) -> Path:
        return (self.project_dir / self.log_dir).resolve()

    @property
    def is_sim(self) -> bool:
        return self.profile == "sim"

    @property
    def connect_cmd(self) -> str:
        if self.console == "minicom":
            return f"minicom -D {self.device} -b {self.baud}"
        return f"{self.device} @ {self.baud}（termios 直连）"

    def start_cmd(self) -> str:
        return self.cmd_start.format(iface=self.interface, file=self.remote_file)

    def size_cmd(self) -> str:
        return self.cmd_size.format(file=self.remote_file)

    def cmd_prefix(self) -> str:
        """需要先 cd 到远端目录时加的前缀。"""
        if self.remote_dir.strip():
            return f"cd {self.remote_dir.strip()} && "
        return ""

    # ---------- 读写 ----------
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = Path(path or DEFAULT_CONFIG_PATH)
        cfg = cls()
        if not path.exists():
            cfg.save(path)
            return cfg
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        for key, value in data.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        cfg.profile = str(cfg.profile).lower()
        cfg.console = str(cfg.console).lower()
        cfg.baud = int(cfg.baud)
        cfg.poll_interval = max(0.2, float(cfg.poll_interval))
        cfg.auto_connect = _bool(cfg.auto_connect)
        if cfg.sim_root == "":
            cfg.sim_root = str(cfg.project_dir / "runtime")
        return cfg

    def save(self, path: Path | None = None) -> Path:
        path = Path(path or DEFAULT_CONFIG_PATH)
        if not path.exists():
            path.write_text(DEFAULT_TOML, encoding="utf-8")
        return path

    def as_dict(self) -> dict:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, Path):
                data[key] = str(value)
        return data


def ensure_dirs(cfg: Config) -> None:
    os.makedirs(cfg.logs_dir, exist_ok=True)
    if cfg.is_sim and cfg.sim_root:
        os.makedirs(cfg.sim_root, exist_ok=True)

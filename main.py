#!/usr/bin/env python3
"""LiDAR 采集控制台入口。

    python3 main.py                 # 默认模拟模式，直接看界面
    python3 main.py --profile real  # 接真机（/dev/ttyUSB0 + eth1）
    python3 main.py --demo          # 自动演示一遍：连接→录制→停止

配置文件默认在 config.toml（不存在则用内置默认值），命令行参数的优先级最高。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lidar_capture import ui  # noqa: E402
from lidar_capture.config import DEFAULT_CONFIG_PATH, Config, ensure_dirs  # noqa: E402


def build_config(argv=None) -> tuple[Config, bool]:
    parser = argparse.ArgumentParser(description="LiDAR pcap 采集控制台")
    parser.add_argument("--profile", choices=["sim", "real"], help="sim=假 3588 演示，real=接真机")
    parser.add_argument("--device", help="串口设备，例如 /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, help="波特率，默认 1500000")
    parser.add_argument("--interface", help="录制网口，默认 eth1")
    parser.add_argument("--console", choices=["minicom", "native"], help="串口后端：外部 minicom 或内置 termios")
    parser.add_argument("--scenario", choices=["normal", "no_traffic", "stall_then_grow", "stuck", "dmesg_fail"],
                        help="模拟场景（只在 sim 模式生效）")
    parser.add_argument("--poll", type=float, help="pcap 采样间隔秒数")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="配置文件路径")
    parser.add_argument("--demo", action="store_true", help="启动后自动演示一遍")
    parser.add_argument("--dump-config", action="store_true", help="打印当前配置后退出")
    args = parser.parse_args(argv)

    if args.config.exists():
        cfg = Config.load(args.config)
    else:
        cfg = Config()

    for key in ("profile", "device", "baud", "interface", "console", "scenario", "poll"):
        value = getattr(args, key)
        if value is not None:
            setattr(cfg, "sim_scenario" if key == "scenario" else key, value)
    ensure_dirs(cfg)

    if args.dump_config:
        import json

        print(json.dumps(cfg.as_dict(), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    return cfg, args.demo


def main(argv=None) -> int:
    cfg, demo = build_config(argv)
    return ui.run(cfg, demo=demo)


if __name__ == "__main__":
    sys.exit(main())

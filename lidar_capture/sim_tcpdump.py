"""模拟的 tcpdump：只复刻这个流程里会被观察到的行为。

  * 收到 -w FILE 后用 "wb" 打开（即覆盖上一段，和真 tcpdump 一致）
  * 先写 24 字节 pcap 全局头，之后按 SIM_RATE 字节/秒持续追加记录
  * 收到 SIGTERM/SIGINT 立刻停止写入（文件保留已写大小）
  * 把自己的进程名改成 tcpdump，让 pgrep/pkill 与真机表现一致

场景（环境变量 SIM_SCENARIO）：
  normal          正常录
  no_traffic      只写头，不再增长（模拟“启动了但还没上包”）
  stall_then_grow 前 SIM_DELAY 秒不增长，然后开始增长
  start_fail      直接报错退出（模拟没权限/网口不存在）
"""

from __future__ import annotations

import os
import signal
import struct
import sys
import time

NAME = "tcpdump"


def set_comm(name: str) -> None:
    try:
        with open("/proc/self/comm", "w", encoding="ascii") as fh:
            fh.write(name)
    except OSError:
        pass


def parse_args(argv: list[str]) -> tuple[str | None, str | None]:
    iface = None
    out = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "-i" and i + 1 < len(argv):
            iface = argv[i + 1]
            i += 2
            continue
        if arg == "-w" and i + 1 < len(argv):
            out = argv[i + 1]
            i += 2
            continue
        if arg.startswith("-i"):
            iface = arg[2:]
        elif arg.startswith("-w"):
            out = arg[2:]
        i += 1
    return iface, out


CLASSIC_HEADER = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
RECORD_PAYLOAD = 1440


def main() -> int:
    set_comm(NAME)
    scenario = os.environ.get("SIM_SCENARIO", "normal")
    rate = float(os.environ.get("SIM_RATE", "2000000"))
    delay = float(os.environ.get("SIM_DELAY", "6"))

    iface, out = parse_args(sys.argv[1:])
    if scenario == "start_fail" or not out:
        msg = "tcpdump: eth1: You don't have permission to capture on that device" if out else "tcpdump: output file required"
        try:
            print(msg, file=sys.stderr)
        except OSError:
            pass
        return 1

    stopping = {"flag": False}

    def on_term(signum, frame):  # noqa: ARG001
        stopping["flag"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, on_term)
        except (OSError, ValueError):
            pass
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (OSError, ValueError):
        pass

    try:
        print(f"tcpdump: listening on {iface or 'eth1'}, link-type EN10MB (Ethernet)", file=sys.stderr)
    except OSError:
        pass

    fh = open(out, "wb")  # 覆盖：新的一段录制从 0 开始
    fh.write(CLASSIC_HEADER)
    fh.flush()

    started = time.monotonic()
    tick = 0.1
    per_tick = max(1, int(rate * tick / RECORD_PAYLOAD))
    record = struct.pack("<IIII", 0, 0, RECORD_PAYLOAD, RECORD_PAYLOAD) + b"\x00" * RECORD_PAYLOAD
    next_at = started + tick
    try:
        while not stopping["flag"]:
            now = time.monotonic()
            if scenario == "stall_then_grow" and (now - started) < delay:
                time.sleep(0.05)
                continue
            if scenario == "no_traffic":
                time.sleep(0.1)
                continue
            if now < next_at:
                time.sleep(min(0.05, next_at - now))
                continue
            sec = int(now)
            usec = int((now - sec) * 1_000_000)
            blob = bytearray()
            for _ in range(per_tick):
                blob += struct.pack("<IIII", sec, usec, RECORD_PAYLOAD, RECORD_PAYLOAD)
                blob += record[16:]
            fh.write(blob)
            fh.flush()
            next_at += tick
    finally:
        try:
            fh.flush()
            fh.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# 演示模式：在本机 pty 上跑一个假 3588，不需要任何硬件。参数照旧可用，例如：
#   ./run_demo.sh --scenario no_traffic    看“命令发出但 pcap 不变大”
#   ./run_demo.sh --demo                   自动演一遍连接→录制→停止
set -e
cd "$(dirname "$0")"
exec python3 main.py --profile sim "$@"

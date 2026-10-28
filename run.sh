#!/usr/bin/env bash
# 工作模式：接真的 3588（串口 + eth1）。参数可以继续覆盖默认值，例如：
#   ./run.sh --device /dev/ttyUSB1 --interface eth0
set -e
cd "$(dirname "$0")"
exec python3 main.py --profile real "$@"

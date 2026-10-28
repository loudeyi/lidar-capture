# LiDAR Capture Console

English | [简体中文](README_zh.md)

[![tests](https://github.com/loudeyi/lidar-capture/actions/workflows/tests.yml/badge.svg)](https://github.com/loudeyi/lidar-capture/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A GUI for the manual workflow we used to run on every drone LiDAR flight — **plug in → `minicom` → silence the kernel log → start `tcpdump` → check whether the pcap is growing → unplug and fly → plug back in → kill `tcpdump` → confirm it really stopped** — with live progress instead of guesswork.

The rules are explicit, not vibes:

- **pcap keeps growing = recording**
- **`pgrep -x tcpdump` finds nothing on the remote side AND `ll` shows an unchanged size = stopped**
- Unplugging the cable does not disturb the 3588 in flight: it keeps recording. On reconnect the app checks the remote process first and, if it is still running, picks the session back up instead of starting over.

Written in Python + Tkinter, **no third-party packages**. The real and simulated links share every judgement rule; only the transport differs.

## Screenshots

![Connected](docs/shots/01-connected.png)

| Recording | Stopped |
| --- | --- |
| ![Recording](docs/shots/02-recording.png) | ![Stopped](docs/shots/05-stopped.png) |

When the cable is pulled the status bar turns red ("not connected"), yet the 3588 keeps recording and the timer keeps running; clicking **Reconnect** takes the session back:

| Unplugged | Reconnected |
| --- | --- |
| ![Unplugged](docs/shots/03-disconnected.png) | ![Reconnected](docs/shots/04-reconnected.png) |

The window sizes itself to the screen (up to 1640×1080); the buttons also have shortcuts — `F5` start, `F6` stop, `F7` reconnect.

## Two entry points

| Script | Mode | Purpose |
| --- | --- | --- |
| `./run.sh` | `--profile real` | **Work mode**: the real 3588 (`/dev/ttyUSB0` + `eth1`) |
| `./run_demo.sh` | `--profile sim` | **Demo mode**: a fake 3588 on a local pty, no hardware needed |

Both pass their arguments straight to `main.py`, so defaults can be overridden:

```bash
./run.sh --device /dev/ttyUSB1 --interface eth0   # another serial port / NIC
./run_demo.sh --scenario no_traffic               # "command sent but pcap never grows"
./run_demo.sh --demo                              # auto-play: connect → record → stop
```

> `python3 main.py` works too and defaults to `sim` for development; use `./run.sh` for real captures.

## Requirements

- Python 3.8+ with Tkinter (Debian/Ubuntu: `sudo apt install python3-tk`)
- Work mode additionally needs `minicom`, `tcpdump` on the 3588, and passwordless `sudo dmesg -D` / `sudo pkill tcpdump`
- The 3588 drops straight into a shell over the serial cable; its working directory is where `lidar.pcap` lives

```bash
git clone https://github.com/loudeyi/lidar-capture.git
cd lidar-capture
./run_demo.sh          # try the demo first
./run.sh               # capture from the real device
```

## The four steps and their checks

| Step | Command | Success criterion |
| --- | --- | --- |
| ① Connect | `minicom -D /dev/ttyUSB0 -b 1500000` | the 3588 returns a lone `__PING__` line |
| ② Silence | `sudo dmesg -D` | exit code 0 — otherwise recording is blocked |
| ③ Record | `nohup tcpdump -i eth1 -w lidar.pcap &` | `ll lidar.pcap` every 2 s shows steady growth |
| ④ Stop | `sudo pkill tcpdump` | `pgrep -x tcpdump` is gone and the `ll` size stops changing |

- Every command is wrapped in three marker lines — `echo __B<n>__ … echo __RC<n>__ $? … echo __E<n>__` — matched whole-line, so noise on the serial console cannot confuse the parser.
- Stopping retries for up to `stop_timeout` (12 s by default), showing the attempt number, process state and pcap size in the event pane; if it cannot be confirmed the UI says **stop failed** instead of pretending.
- The "serial output" pane only keeps things a human wants: the boot banner, the `dmesg` result, the `[1] 101` job notice, and **one evidence line whenever the verdict changes**, e.g. `ll lidar.pcap → 3.07 MB (growing)`. Protocol markers and polling echo are filtered out.
- The capture file is always `lidar.pcap`, **overwritten** on each new session, together with the previous session's logs.

## Demo mode actually runs something

Not an animation: it spawns a real `bash` on a `pty` with stub `tcpdump / pgrep / pkill / sudo / dmesg / ll` on `PATH`, and the pcap is a real, growing file. "Unplug" closes the pty master — the stub `tcpdump` survives thanks to `nohup`, so after "re-plugging" a fresh bash still finds it with `pgrep`, exactly like the real thing.

Built-in scenarios: `normal`, `no_traffic` (command sent, nothing captured), `stall_then_grow` (stalls first, then starts), `stuck` (`pkill` refuses to die), `dmesg_fail` (step ② fails).

## Logs and artifacts

Everything lives in the project directory and is **overwritten at the start of each session**, just like the pcap:

```
logs/session.log    full serial transcript (including the pre-flight connect phase)
logs/events.jsonl   structured events: link_open / start / recording / stopped / stop_failed …
logs/summary.md     session result: duration, size, average rate, reason
runtime/lidar.pcap  the fake 3588's capture file in demo mode
```

## Configuration

A `config.toml` is created in the project root on first run (already ignored by git):

| Key | Default | Meaning |
| --- | --- | --- |
| `profile` | `sim` | `sim` / `real`; the entry scripts override it |
| `device` / `baud` | `/dev/ttyUSB0` / `1500000` | serial port |
| `interface` | `eth1` | NIC captured by tcpdump |
| `remote_file` / `remote_dir` | `lidar.pcap` / empty | remote file name and directory |
| `console` | `minicom` | `minicom` (external) or `native` (built-in termios) |
| `auto_connect` | `true` | run `sudo dmesg -D` right after connecting |
| `poll_interval` | `2.0` | pcap sampling interval, seconds |
| `start_timeout` / `stop_timeout` | `30` / `12` | wait for packets / wait for stop |

Command-line flags win over the config file, e.g. `./run.sh --poll 1`.

## Tests

```bash
python3 -m unittest discover -s tests -v     # 44 tests, ~23 s
```

- Unit tests: config round-trip, size sliding window, marker parsing, fake shell, `ll` output parsing, protocol echo filtering, evidence-line rules
- End-to-end (`tests/test_sim_integration.py`): happy path → stop confirmation → log overwrite → unplug/replug → stuck stop reported as failure → start timeout when no packets arrive → `dmesg` failure blocks recording

CI runs the same suite on GitHub Actions (`.github/workflows/tests.yml`).

## Layout

```
main.py                       entry point (args → config → GUI)
run.sh / run_demo.sh          work mode / demo mode
lidar_capture/config.py       config and command templates
lidar_capture/protocol.py     serial protocol: markers, timeouts, unplug detection
lidar_capture/transports.py   minicom / termios / simulated links
lidar_capture/sim.py          fake 3588 (pty + real bash + stub commands)
lidar_capture/sim_tcpdump.py  fake tcpdump (writes a real pcap)
lidar_capture/controller.py   state machine and all the judgement
lidar_capture/monitor.py      size sliding window
lidar_capture/events.py       session logs (overwrite semantics)
lidar_capture/ui.py           Tkinter UI
tools/shoot.py                automated screenshots
docs/shots/                   screenshots
tests/                        unit tests + simulated end-to-end tests
```

## FAQ

- **Step ② keeps failing**: `sudo` needs a password on the 3588 — run `sudo -v` once or configure passwordless sudo.
- **`/dev/ttyUSB0` cannot be opened**: `minicom` already holds it, or the device name differs (`ls /dev/ttyUSB*`).
- **`eth1` does not exist**: check `ip link` and pass the real interface with `--interface`.
- **Window too big or too small**: it defaults to 94% of the screen (max 1640×1080); dragging the edges works too.
- **Want to type commands yourself?** The UI only exposes the four steps; for debugging call `controller.manual("…")`.

## License

[MIT](LICENSE) © 2026 loudeyi

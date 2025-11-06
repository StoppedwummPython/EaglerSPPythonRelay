# 🛰️ EaglerSPRelay (Python Edition)

A Python-based WebSocket relay server for **Eaglercraft Shared Worlds**, written for learning, testing, or standalone hosting purposes.
This script mimics the behavior of the Java-based `EaglerSPRelay` — a shared world connection manager that allows **clients and servers to connect, exchange ICE candidates, descriptions, and session data via a relay system.**

---

## 📦 Features

* Full asynchronous WebSocket relay system (powered by `asyncio` + `websockets`)
* Dynamic **STUN/TURN relay loading** via `relays.txt`
* Configurable server via `relayConfig.ini`
* **Rate limiting** for world and ping connections
* Local-world listing per IP
* Real IP forwarding support via HTTP headers
* Command-line controls: `stop`, `reset`
* Cross-platform (Windows, Linux, macOS)

---

## ⚙️ Requirements

* **Python 3.10+**
* `websockets` package (for the WebSocket server)

### Install dependencies

```bash
pip install websockets
```

Optional (for development):

```bash
pip install mypy pylint
```

---

## 🚀 Running the Relay

### Run directly:

```bash
python EaglerSPRelay.py
```

The server will:

* Load (or create) the `relayConfig.ini` file
* Load (or create) `relays.txt` with STUN/TURN servers
* Start listening on the configured address and port (default: `0.0.0.0:6699`)
* Print relay status and await connections

---

## 💡 Console Commands

When running, type the following commands into the console:

| Command        | Description                                                            |
| -------------- | ---------------------------------------------------------------------- |
| `stop` / `end` | Gracefully stops the relay server                                      |
| `reset`        | Clears all rate limiter states (unblocks temporarily rate-limited IPs) |

---

## 🧩 Configuration

### `relayConfig.ini`

This file controls the relay server’s behavior. It’s auto-generated on first launch.

Example:

```ini
[EaglerSPRelay]
address = 0.0.0.0
port = 6699
code-length = 5
code-chars = abcdefghijklmnopqrstuvwxyz0123456789
code-mix-case = False
connections-per-ip = 128
worlds-per-ip = 32
world-ratelimit-enable = True
world-ratelimit-period = 192
world-ratelimit-limit = 32
world-ratelimit-lockout-limit = 48
world-ratelimit-lockout-duration = 600
ping-ratelimit-enable = True
ping-ratelimit-period = 256
ping-ratelimit-limit = 128
ping-ratelimit-lockout-limit = 192
ping-ratelimit-lockout-duration = 300
origin-whitelist = 
enable-real-ip-header = False
real-ip-header-name = X-Real-IP
show-local-worlds = True
server-comment = Eags. Shared World Relay
```

> 💬 Adjust port, origin whitelist, and ratelimit behavior as needed.

---

## 🌐 Relay Configuration (`relays.txt`)

This file defines **STUN** and **TURN** servers that are passed to Eaglercraft clients for peer connection establishment.

Example:

```ini
# Example relays.txt
[STUN]
url=stun:stun.l.google.com:19302

[TURN]
url=turn:turn.yourserver.com:3478
username=user
password=pass
```

Supported tags:

* `[STUN]` or `[NO_PASSWD]` – STUN relay (no credentials)
* `[TURN]` or `[PASSWD]` – TURN relay (requires credentials)

---

## 🧠 Architecture Overview

```
Clients ↔ EaglerSPRelay ↔ Servers
```

### Key Components

| Class                 | Purpose                                                                           |
| --------------------- | --------------------------------------------------------------------------------- |
| `EaglerSPRelay`       | Core relay manager; handles all WebSocket connections, routing, and configuration |
| `EaglerSPServer`      | Represents a shared world host connected to the relay                             |
| `EaglerSPClient`      | Represents a joining client connecting to a host                                  |
| `RelayPacket*`        | Packet structures (handshake, ICE, description, success/failure, etc.)            |
| `EaglerSPRelayConfig` | Manages `relayConfig.ini` settings and code generation                            |
| `RelayList`           | Loads STUN/TURN relays from `relays.txt`                                          |
| `RateLimiter`         | Handles ratelimit logic per IP address                                            |
| `LoginState`          | Tracks client/server connection state machine                                     |

---

## 🧾 Logging

The relay outputs logs in the following format:

```
[12:45:03][MainThread/INFO][EaglerSPRelay]: EaglerSPRelay listening on 0.0.0.0:6699
```

Default log level: **INFO**

Debug logs (for packet warnings, ratelimit triggers, etc.) are shown if you modify:

```python
logging.basicConfig(level=logging.DEBUG)
```

---

## 🔒 Security Notes

* Always configure a **trusted origin whitelist** in production:

  ```ini
  origin-whitelist = https://yourdomain.com; https://another.com
  ```
* If your relay is behind a reverse proxy, enable:

  ```ini
  enable-real-ip-header = True
  real-ip-header-name = X-Forwarded-For
  ```

---

## 🧰 Troubleshooting

| Problem                        | Possible Cause              | Fix                                                         |
| ------------------------------ | --------------------------- | ----------------------------------------------------------- |
| Clients can’t connect          | Wrong origin / port blocked | Add domain to `origin-whitelist`, check firewall            |
| “Too many connections”         | IP rate limit reached       | Increase `connections-per-ip`                               |
| Relays not loading             | Missing `relays.txt`        | Restart relay — it will auto-generate one                   |
| Outdated client/server message | Protocol version mismatch   | Ensure both sides use the same `Constants.PROTOCOL_VERSION` |

---

## 🧑‍💻 License

This project is provided **for educational and testing purposes** only.
Original Eaglercraft code © LAX1DUDE.
Python rewrite © Stoppedwumm (2025).

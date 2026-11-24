# ZKT Relay

A small program for an always-on Windows PC on the office network. It lets a
push-only ZKTeco device (for example a SenseFace 2A) keep feeding the server it
already uses, such as **ZKTeco BioTime**, while ERPNext also gets every punch.

```
Device --> relay PC (port 8000) --> BioTime            (unchanged; BioTime answers the device)
                                \-> ERPNext site(s)    (copy of each punch upload, to every target)
```

A push device sends to one server only, and an ERPNext site on the internet
cannot reach a LAN address such as BioTime's, so the relay sits on the LAN:

- Every request goes to BioTime unchanged, and BioTime's answer goes back to the
  device. BioTime sees no difference.
- Each punch upload (ATTLOG) of the listed serial numbers is saved in `archive\`
  (one folder per month) and in one queue per ERPNext target (`queue\<name>\`),
  then delivered to every target with retries. Targets are independent: one being
  offline (a laptop that is switched off, an internet outage) never delays another,
  and its punches wait in its queue until it is back.
- Requests to ERPNext carry the device's **Relay Token** (`X-ZKT-Relay-Token`).
  ERPNext refuses punches for a relay device without it. BioTime never sees it.
- The relay passes the device's heartbeat on to each target every few minutes, and
  only while that target's queue is empty. ERPNext counts only that heartbeat as the
  device's last contact, so Shift Type *Last Sync of Checkin* never moves past punches
  still waiting.

Standard library only (Python 3.8+); `install.ps1` downloads a private Python copy.
An older `config.json` with a single `erpnext_url` / `erpnext_site` / `erpnext_token`
still works (one target named `erpnext`).

## ERPNext side

In **ZKT Settings > Devices**: *Connection* = Push, the device's *Serial Number*,
tick **Via Office Relay**, and set a **Relay Token** (16+ characters).

## Install on the relay PC

1. Copy this folder to the PC.
2. Copy `config.example.json` to `config.json` and fill it in:

   | Key | Meaning |
   |---|---|
   | `listen_port` | port the device sends to (default 8000) |
   | `device_ip` | the device's IP; the firewall opens the port for it only |
   | `biotime_url` | the server the device fed before, e.g. `http://192.168.1.10:8085` |
   | `erpnext_targets` | one entry per ERPNext site: `name` (letters, digits, `-`, `_`), `url`, `site` (site name, only when `url` is an IP address, e.g. a bench reached by IP), `token` (the device's Relay Token in that site) |
   | `serials` | serial numbers whose punches go to ERPNext |

3. As Administrator, in this folder:

   ```
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```

   It installs the program in `C:\zkt_relay` with a private Python in
   `C:\zkt_relay\python` (nothing system-wide), a firewall rule that opens the port
   for `device_ip` only, and a scheduled task *ZKT Relay* that runs as SYSTEM, starts
   with Windows and restarts if it stops. Running it again updates the program; a
   different `config.json` already there is kept as `config.json.bak-<date>`.

4. On the device: **COMM. > Cloud Server Setting**, change only *Server Address*
   (the relay PC's IP) and *Server Port*. Write down the old values first.

Give the relay PC a fixed IP (a DHCP reservation): the device sends to that address.

## Check

```
curl http://localhost:8000/health
```

Shows the device's last call, BioTime's last answer and, per target, the last
delivery (`last_ok`, `last_error`) and the uploads waiting in its queue.
Details in `C:\zkt_relay\relay.log`.

## Send archived punches again

For example after adding a target (only that target gets them with `--target`):

```
C:\zkt_relay\python\python.exe C:\zkt_relay\zkt_relay.py --replay-archive --since 2026-01-01 --target production
```

The running relay delivers them within a minute; ERPNext skips punches it already has.

## Remove

1. Point the device back to the server it fed before (old Cloud Server values).
2. `powershell -ExecutionPolicy Bypass -File uninstall.ps1`

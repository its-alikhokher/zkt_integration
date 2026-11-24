# ZKT Relay

A small program for an always-on Windows PC on the office network. It lets a
push-only ZKTeco device (for example a SenseFace 2A) keep feeding the server it
already uses, such as **ZKTeco BioTime**, while ERPNext also gets every punch.

```
Device --> relay PC (port 8000) --> BioTime         (unchanged; BioTime answers the device)
                                \-> ERPNext /iclock  (copy of each punch upload)
```

A push device sends to one server only, and an ERPNext site on the internet
cannot reach a LAN address such as BioTime's, so the relay sits on the LAN:

- Every request goes to BioTime unchanged, and BioTime's answer goes back to the
  device. BioTime sees no difference.
- Each punch upload (ATTLOG) of the listed serial numbers is first saved in
  `queue\`, then delivered to ERPNext with retries. An ERPNext or internet
  outage loses nothing. Delivered uploads move to `archive\` (one folder per month).
- Requests to ERPNext carry the device's **Relay Token** (`X-ZKT-Relay-Token`).
  ERPNext refuses punches for a relay device without it. BioTime never sees it.
- The relay passes the device's heartbeat on to ERPNext every few minutes, and only
  while its queue is empty. ERPNext counts only that heartbeat as the device's last
  contact, so Shift Type *Last Sync of Checkin* never moves past punches still waiting.

Standard library only (Python 3.8+); `install.ps1` downloads a private Python copy.

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
   | `erpnext_url` | the ERPNext site, e.g. `https://erp.example.com` |
   | `erpnext_site` | site name, only when `erpnext_url` is an IP address (a bench reached by IP) |
   | `erpnext_token` | the device's Relay Token in ZKT Settings |
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

Shows the device's last call, BioTime's last answer, ERPNext delivery
(`erpnext_last_ok`, `erpnext_last_error`) and uploads waiting in the queue.
Details in `C:\zkt_relay\relay.log`.

## Send archived punches again

After pointing the relay at another ERPNext (for example from staging to
production):

```
C:\zkt_relay\python\python.exe C:\zkt_relay\zkt_relay.py --replay-archive --since 2026-01-01
```

The running relay delivers them within a minute; ERPNext skips punches it already has.

## Remove

1. Point the device back to the server it fed before (old Cloud Server values).
2. `powershell -ExecutionPolicy Bypass -File uninstall.ps1`

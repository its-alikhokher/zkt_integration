# ZKT Biometrix Integration

A Frappe app for **Frappe HRMS** that pulls attendance punches from **ZKTeco biometric
devices** (F22, K40, and other devices that speak the ZK SDK protocol on port 4370) and
turns them into **Employee Checkin** records in ERPNext.

## How it works

Every run does this for each device listed in **ZKT Settings**:

1. Collect the **Attendance Device ID** of every **Active** Employee.
2. Pull the attendance log from the device and keep only punches of those IDs.
   Punches of unknown or inactive users are ignored (nothing is stored for them).
3. Create one **Employee Checkin** per new punch, with log type **IN** or **OUT**.
   A punch that already exists (same employee, same time) is skipped, so running the
   sync repeatedly never creates duplicates.

Everything the sync does is written to the **Attendance Device Log** doctype.

## Requirements

- Frappe / ERPNext / HRMS version 15 or 16
- Python package `pyzk` (installed automatically with the app)
- The device must be reachable from the bench server on TCP port **4370**
- Background workers running (`bench start` in development, supervisor in production)

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench --site your-site install-app zkt_integration
bench --site your-site migrate
```

## Updating

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop   # or: cd apps/zkt_integration && git pull
bench --site your-site migrate
bench --site your-site clear-cache
bench restart                                       # production only
```

`migrate` runs the app's patches and registers the scheduled jobs from `hooks.py`.
Upgrading from an older version also:

- removes the old **Server Script** fixtures ("Schedule Attendance Pull",
  "Clear Attendance Pull Logs"); the jobs now live in `hooks.py`, so
  `server_script_enabled` is no longer needed in the bench config;
- drops the old **ZKT Raw Attendance** doctype and its table; the sync writes
  Employee Checkin directly.

## Setup

### 1. ZKT Settings

Open **ZKT Settings** and add one row per device:

| Field | Meaning |
|---|---|
| Device ID | Any name, e.g. `main-entrance`. Stored on each Employee Checkin as `device_id`. |
| Device IP Address | IP of the device, e.g. `192.168.1.201`. Port 4370 is assumed. |
| Device Password | The device's **numeric** communication key. Leave empty when the device has none (most do not). |
| Punch Direction | `AUTO` (recommended), `IN` / `OUT` for a device that only records one direction, `None` to leave the log type empty. See [IN / OUT](#in--out). |
| Clear From Device On Fetch | Reserved. Clearing is currently disabled in code. |
| Latitude / Longitude | Copied to each Employee Checkin. |
| Pull Frequency | Minutes the scheduler waits between two pulls of this device. |
| Last Run | Set automatically after each run. Clear it to force the next scheduled run. |

Save the settings.

### 2. Employees

For every person on the device, the Employee record in ERPNext needs
**Attendance Device ID (Biometric/RF tag ID)** = the user ID on the device
(the number shown as "User ID" on the device, not the name).
Only **Active** employees are synced.

To see the device's user list, run this from the bench folder (replace the IP):

```bash
env/bin/python -c "
from zk import ZK
c = ZK('192.168.1.201', port=4370, password=0).connect()
for u in c.get_users(): print(u.user_id, '-', u.name)
c.disconnect()"
```

### 3. Shift Type

In each **Shift Type** set *Determine Check-in and Check-out* to
**Alternating entries as IN and OUT during the same shift**. This works whether or
not the device sends a punch state, and is what the derived IN / OUT logic assumes.

## Running the sync

**Button.** Open **ZKT Settings** and click **Sync Attendance Now**. The sync runs as
a background job on the `long` queue, ignores the pull frequency, and reports to
**Attendance Device Log** (there is a button for it next to the sync button).
If a sync is already queued or running, the button tells you so instead of
starting a second one.

**Scheduler.** The sync runs automatically every hour (`hourly_long` in `hooks.py`)
and respects each device's *Pull Frequency*. Make sure the scheduler is enabled:

```bash
bench --site your-site scheduler enable
```

and that `pause_scheduler` is not `1` in `sites/common_site_config.json`.

**Command line.**

```bash
bench --site your-site execute zkt_integration.zkt_biometrix_integration.script.sync_attendance_log_to_erpnext
# ignore the pull frequency:
bench --site your-site execute zkt_integration.zkt_biometrix_integration.script.sync_attendance_log_to_erpnext --kwargs "{'force': 1}"
```

## IN / OUT

The device records a *punch state* with every punch when its **Punch State** option
is on (on an F22: Menu → System → Attendance → Punch State Options → Manual or Auto
mode). The app maps state `0` and `4` to **IN**, `1` and `5` to **OUT**.

Many devices are left with punch state **Off** and send `255` (no state) for every
punch. Then, with Punch Direction = `AUTO`, the app derives the direction per employee:

- first punch is **IN**, the next is **OUT**, the next **IN**, and so on;
- an IN older than **14 hours** counts as a forgotten OUT, so the next punch starts a
  new IN;
- a second punch within **2 minutes** of the previous one is a duplicate and is ignored.

The two limits are `MAX_SHIFT_HOURS` and `DUPLICATE_WINDOW_SECONDS` at the top of
`zkt_integration/zkt_biometrix_integration/script.py`.

Existing Employee Checkins are never changed. If you want IN / OUT on check-ins that
were created before this logic existed, delete them and run the sync again; the device
still holds the punches.

## Logs

Every run writes to **Attendance Device Log**. The list view has a **Clear Logs**
button, and the table is emptied automatically once a week (`weekly` in `hooks.py`).

## Troubleshooting

| Message in Attendance Device Log | Meaning / fix |
|---|---|
| `No Active Employee has an Attendance Device ID; skipping device …` | No Employee has the Attendance Device ID field filled, or they are not Active. See [Employees](#2-employees). |
| `… ignored (no Active Employee with that ID)` | Punches of device users who have no Active Employee. Map them to see their punches. |
| `Skipping device …; pull frequency not met` | Scheduled run came too early. Use the button, `--kwargs "{'force': 1}"`, or clear *Last Run*. |
| `[DEVICE ERROR] … ` | Device not reachable: check IP, that port 4370 is open, and that no other SDK client is holding the connection. |
| `[DEVICE WARNING] … device password must be numeric, using 0` | The Device Password field holds text. Clear it, or enter the numeric comm key. |
| `[ERP ERROR] … Transactions cannot be created for an Inactive Employee` | HRMS rejected the punch; the Employee is not Active. |
| Nothing happens after clicking the button | Background workers are not running (`bench worker` / supervisor), or the scheduler queue is paused. |

## Notes on devices

- Device memory is limited (an F22 holds 30,000 punches). The app never clears the
  device; delete old records on the device itself when it fills up.
- The app can be used alongside ZKTeco BioTime / ADMS push mode. Both read the same
  log; the app connects directly over port 4370.
- The device's clock is used as-is; keep it in sync with the server's timezone.

## License

MIT

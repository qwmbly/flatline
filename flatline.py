#!/usr/bin/env python3
"""Flatline - SMART drive monitoring wrapper for smartmontools."""

import argparse
import json
import logging
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/opt/flatline/config.toml")
DEFAULT_STATE_PATH = Path("/opt/flatline/state.json")
DEFAULT_DB_PATH = Path("/opt/flatline/history.db")

TRACKED_ATTRS = {
    5: "Reallocated_Sector_Ct",
    197: "Current_Pending_Sector",
    198: "Offline_Uncorrectable",
}

DEFAULTS = {
    "email": {
        "to": "",
        "from": "flatline@nas.local",
        "subject_prefix": "[FLATLINE]",
    },
    "thresholds": {
        "temperature_max": 55,
    },
    "drives": {
        "exclude": [],
    },
    "paths": {
        "msmtp": "",
        "smartctl": "",
        "state_file": str(DEFAULT_STATE_PATH),
        "history_db": str(DEFAULT_DB_PATH),
    },
}

log = logging.getLogger("flatline")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    """Load and merge config with defaults."""
    config = {}
    if path.exists():
        with open(path, "rb") as f:
            config = tomllib.load(f)

    merged = {}
    for section, defaults in DEFAULTS.items():
        merged[section] = {**defaults, **(config.get(section, {}))}

    return merged


def find_binary(name: str, configured_path: str) -> str:
    """Resolve a binary path from config or PATH lookup."""
    if configured_path:
        p = Path(configured_path)
        if p.is_file():
            return str(p)
        log.warning("Configured path %s for %s not found, falling back to PATH", configured_path, name)

    found = shutil.which(name)
    if found:
        return found

    log.error("Could not find %s. Is it installed?", name)
    sys.exit(1)


# ---------------------------------------------------------------------------
# smartctl interaction
# ---------------------------------------------------------------------------

def run_smartctl(smartctl: str, args: list[str]) -> dict | None:
    """Run smartctl with --json and return parsed output, or None on failure."""
    cmd = [smartctl, "--json=c"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        log.error("smartctl timed out: %s", " ".join(cmd))
        return None

    # smartctl uses bitmask exit codes; some non-zero codes are informational.
    # The JSON output is still valid in most cases.
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("Failed to parse smartctl JSON output for: %s\nstderr: %s", " ".join(cmd), result.stderr)
        return None


def scan_drives(smartctl: str) -> list[dict]:
    """Discover drives via smartctl --scan."""
    data = run_smartctl(smartctl, ["--scan-open"])
    if not data:
        return []

    devices = []
    for dev in data.get("devices", []):
        devices.append({
            "name": dev.get("name", ""),
            "type": dev.get("type", ""),
        })
    return devices


def get_smart_data(smartctl: str, device: str, dev_type: str = "") -> dict | None:
    """Get full SMART data for a single device."""
    args = ["-a", device]
    if dev_type:
        args.extend(["-d", dev_type])

    data = run_smartctl(smartctl, args)
    if not data:
        return None

    # Extract the fields we care about
    info = data.get("model_name", data.get("scsi_model_name", "Unknown"))
    serial = data.get("serial_number", "Unknown")

    health_obj = data.get("smart_status", {})
    health = "PASSED" if health_obj.get("passed", False) else "FAILED"

    temperature = None
    temp_obj = data.get("temperature", {})
    if temp_obj:
        temperature = temp_obj.get("current")

    # Parse ATA SMART attributes
    attrs = {}
    for attr in data.get("ata_smart_attributes", {}).get("table", []):
        attr_id = attr.get("id")
        if attr_id in TRACKED_ATTRS:
            attrs[TRACKED_ATTRS[attr_id]] = attr.get("raw", {}).get("value", 0)

    # For NVMe drives, map different attribute names
    nvme_health = data.get("nvme_smart_health_information_log", {})
    if nvme_health:
        if temperature is None:
            temperature = nvme_health.get("temperature")
        # NVMe reports media errors and available spare differently
        media_errors = nvme_health.get("media_errors", 0)
        attrs["Media_Errors"] = media_errors

    # Parse self-test log for most recent result
    last_test_status = ""
    self_test_log = data.get("ata_smart_self_test_log", {}).get("standard", {}).get("table", [])
    if self_test_log:
        last_entry = self_test_log[0]
        status = last_entry.get("status", {})
        last_test_status = status.get("string", "")
        if not status.get("passed", True):
            last_test_status = "FAILED: " + last_test_status

    # Also check NVMe self-test log
    nvme_test_log = data.get("nvme_self_test_log", {}).get("table", [])
    if nvme_test_log and not last_test_status:
        last_entry = nvme_test_log[0]
        status = last_entry.get("status", {})
        last_test_status = status.get("string", "")

    return {
        "device": device,
        "model": info,
        "serial": serial,
        "health": health,
        "temperature": temperature,
        "reallocated_sector_ct": attrs.get("Reallocated_Sector_Ct", 0),
        "current_pending_sector": attrs.get("Current_Pending_Sector", 0),
        "offline_uncorrectable": attrs.get("Offline_Uncorrectable", 0),
        "media_errors": attrs.get("Media_Errors"),
        "power_on_hours": data.get("power_on_time", {}).get("hours", 0),
        "last_test_status": last_test_status,
    }


def run_self_test(smartctl: str, device: str, dev_type: str, test_type: str) -> bool:
    """Trigger a self-test (short or long). Returns True on success."""
    args = ["-t", test_type, device]
    if dev_type:
        args.extend(["-d", dev_type])

    data = run_smartctl(smartctl, args)
    if data is None:
        log.error("Failed to start %s self-test on %s", test_type, device)
        return False
    return True


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    """Load previous state from JSON file."""
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not load state file %s: %s", path, e)
        return {}


def save_state(path: Path, state: dict) -> None:
    """Write current state to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.rename(path)


# ---------------------------------------------------------------------------
# History database
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize SQLite database, creating tables if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            timestamp TEXT NOT NULL,
            serial TEXT NOT NULL,
            device TEXT,
            model TEXT,
            health TEXT,
            temperature INTEGER,
            reallocated_sector_ct INTEGER,
            current_pending_sector INTEGER,
            offline_uncorrectable INTEGER,
            media_errors INTEGER,
            power_on_hours INTEGER
        )
    """)
    # Create indexes if they don't exist
    conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_serial ON readings(serial)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings(timestamp)")
    conn.commit()
    return conn


def record_history(conn: sqlite3.Connection, readings: list[dict], timestamp: str) -> None:
    """Append current readings to history database."""
    for r in readings:
        conn.execute(
            """INSERT INTO readings
               (timestamp, serial, device, model, health, temperature,
                reallocated_sector_ct, current_pending_sector,
                offline_uncorrectable, media_errors, power_on_hours)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp,
                r["serial"],
                r["device"],
                r["model"],
                r["health"],
                r["temperature"],
                r["reallocated_sector_ct"],
                r["current_pending_sector"],
                r["offline_uncorrectable"],
                r.get("media_errors"),
                r["power_on_hours"],
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Alert comparison
# ---------------------------------------------------------------------------

def compare_readings(current: list[dict], previous_state: dict, config: dict) -> list[dict]:
    """Compare current readings against previous state. Returns list of alerts."""
    alerts = []
    temp_max = config["thresholds"]["temperature_max"]
    seen_serials = set()

    for reading in current:
        serial = reading["serial"]
        seen_serials.add(serial)
        prev = previous_state.get(serial, {})
        drive_alerts = []

        # Health status
        if reading["health"] != "PASSED":
            drive_alerts.append(f"SMART health status: {reading['health']}")

        # Temperature
        if reading["temperature"] is not None and reading["temperature"] > temp_max:
            drive_alerts.append(
                f"Temperature: {reading['temperature']}C (threshold: {temp_max}C)"
            )

        # Sector counts (alert on increase)
        for attr in ("reallocated_sector_ct", "current_pending_sector", "offline_uncorrectable"):
            cur_val = reading.get(attr, 0) or 0
            prev_val = prev.get(attr, 0) or 0
            if cur_val > prev_val:
                delta = cur_val - prev_val
                name = attr.replace("_", " ").title()
                drive_alerts.append(f"{name}: {prev_val} -> {cur_val} (+{delta})")

        # NVMe media errors (alert on increase)
        if reading.get("media_errors") is not None:
            cur_val = reading["media_errors"]
            prev_val = prev.get("media_errors", 0) or 0
            if cur_val > prev_val:
                delta = cur_val - prev_val
                drive_alerts.append(f"Media Errors: {prev_val} -> {cur_val} (+{delta})")

        # Self-test failure
        test_status = reading.get("last_test_status", "")
        if test_status.startswith("FAILED"):
            drive_alerts.append(f"Self-test: {test_status}")

        if drive_alerts:
            alerts.append({
                "device": reading["device"],
                "model": reading["model"],
                "serial": serial,
                "issues": drive_alerts,
            })

    # Check for missing drives (previously seen but not in current scan)
    for serial, prev in previous_state.items():
        if serial not in seen_serials:
            alerts.append({
                "device": prev.get("device", "unknown"),
                "model": prev.get("model", "unknown"),
                "serial": serial,
                "issues": [
                    f"Drive missing from scan (last seen: {prev.get('last_seen', 'unknown')})"
                ],
            })

    return alerts


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def format_alert_email(alerts: list[dict], config: dict) -> tuple[str, str]:
    """Format alert email. Returns (subject, body)."""
    issue_count = sum(len(a["issues"]) for a in alerts)
    prefix = config["email"]["subject_prefix"]
    subject = f"{prefix} Alert: {issue_count} issue{'s' if issue_count != 1 else ''} detected"

    lines = []
    for alert in alerts:
        lines.append(f"Drive: {alert['model']} (serial: {alert['serial']}, {alert['device']})")
        for issue in alert["issues"]:
            lines.append(f"  - {issue}")
        lines.append("")

    lines.append("---")
    lines.append("Flatline SMART Monitor")

    body = "\n".join(lines)
    return subject, body


def send_email(subject: str, body: str, config: dict, msmtp: str) -> bool:
    """Send email via msmtp."""
    to_addr = config["email"]["to"]
    from_addr = config["email"]["from"]

    if not to_addr:
        log.error("No email recipient configured. Set email.to in config.toml.")
        return False

    message = f"From: {from_addr}\nTo: {to_addr}\nSubject: {subject}\n\n{body}"

    try:
        result = subprocess.run(
            [msmtp, "-a", "default", to_addr],
            input=message,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.error("msmtp failed (exit %d): %s", result.returncode, result.stderr)
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("msmtp timed out sending email")
        return False
    except FileNotFoundError:
        log.error("msmtp not found at %s", msmtp)
        return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_check(config: dict, smartctl: str, msmtp: str) -> int:
    """Run a health check on all drives."""
    state_path = Path(config["paths"]["state_file"])
    db_path = Path(config["paths"]["history_db"])
    exclude = set(config["drives"]["exclude"])

    devices = scan_drives(smartctl)
    if not devices:
        log.error("No drives found. Check that smartmontools is installed and you have permissions.")
        return 1

    timestamp = datetime.now(timezone.utc).isoformat()
    readings = []
    errors = []

    for dev in devices:
        if dev["name"] in exclude:
            log.debug("Skipping excluded drive: %s", dev["name"])
            continue

        data = get_smart_data(smartctl, dev["name"], dev["type"])
        if data is None:
            errors.append(f"Failed to read SMART data from {dev['name']}")
            continue
        readings.append(data)

    # Load previous state and compare
    previous_state = load_state(state_path)
    alerts = compare_readings(readings, previous_state, config)

    # Add smartctl read errors as alerts
    if errors:
        alerts.append({
            "device": "N/A",
            "model": "N/A",
            "serial": "N/A",
            "issues": errors,
        })

    # Record history
    try:
        conn = init_db(db_path)
        record_history(conn, readings, timestamp)
        conn.close()
    except sqlite3.Error as e:
        log.error("Failed to record history: %s", e)

    # Send alerts if any
    if alerts:
        subject, body = format_alert_email(alerts, config)
        log.warning("Sending alert email: %s", subject)
        send_email(subject, body, config, msmtp)

    # Update state
    new_state = {}
    for r in readings:
        new_state[r["serial"]] = {
            "device": r["device"],
            "model": r["model"],
            "last_seen": timestamp,
            "health": r["health"],
            "temperature": r["temperature"],
            "reallocated_sector_ct": r["reallocated_sector_ct"],
            "current_pending_sector": r["current_pending_sector"],
            "offline_uncorrectable": r["offline_uncorrectable"],
            "media_errors": r.get("media_errors"),
            "power_on_hours": r["power_on_hours"],
            "last_test_status": r.get("last_test_status", ""),
        }
    save_state(state_path, new_state)

    if alerts:
        log.info("Check complete: %d alert(s) sent", len(alerts))
    else:
        log.info("Check complete: all drives healthy")

    return 0


def cmd_self_test(config: dict, smartctl: str, msmtp: str, test_type: str) -> int:
    """Trigger self-tests on all drives, then run a check."""
    exclude = set(config["drives"]["exclude"])
    devices = scan_drives(smartctl)

    if not devices:
        log.error("No drives found.")
        return 1

    for dev in devices:
        if dev["name"] in exclude:
            continue
        log.info("Starting %s self-test on %s", test_type, dev["name"])
        run_self_test(smartctl, dev["name"], dev["type"], test_type)

    # Run a normal check afterward to capture any existing issues
    # (test results will appear in a future check once the test completes)
    log.info("Self-tests initiated. Running health check.")
    return cmd_check(config, smartctl, msmtp)


def cmd_status(config: dict, smartctl: str) -> int:
    """Print a human-readable status summary of all drives."""
    exclude = set(config["drives"]["exclude"])
    devices = scan_drives(smartctl)

    if not devices:
        print("No drives found.")
        return 1

    for dev in devices:
        if dev["name"] in exclude:
            continue

        data = get_smart_data(smartctl, dev["name"], dev["type"])
        if data is None:
            print(f"\n{dev['name']}: FAILED TO READ")
            continue

        print(f"\n{dev['name']}: {data['model']} (serial: {data['serial']})")
        print(f"  Health:         {data['health']}")
        print(f"  Temperature:    {data['temperature']}C" if data["temperature"] is not None else "  Temperature:    N/A")
        print(f"  Power-on hours: {data['power_on_hours']}")
        print(f"  Reallocated:    {data['reallocated_sector_ct']}")
        print(f"  Pending:        {data['current_pending_sector']}")
        print(f"  Uncorrectable:  {data['offline_uncorrectable']}")
        if data.get("media_errors") is not None:
            print(f"  Media errors:   {data['media_errors']}")
        if data.get("last_test_status"):
            print(f"  Last test:      {data['last_test_status']}")

    print()
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="flatline",
        description="SMART drive monitoring wrapper for smartmontools",
    )
    parser.add_argument(
        "-c", "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="enable debug logging",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="run a health check on all drives")
    sub.add_parser("short-test", help="trigger short self-tests and check health")
    sub.add_parser("long-test", help="trigger long self-tests and check health")
    sub.add_parser("status", help="print current drive status (human-readable)")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(args.config)
    smartctl = find_binary("smartctl", config["paths"]["smartctl"])
    msmtp = find_binary("msmtp", config["paths"]["msmtp"])

    if args.command == "check":
        return cmd_check(config, smartctl, msmtp)
    elif args.command == "short-test":
        return cmd_self_test(config, smartctl, msmtp, "short")
    elif args.command == "long-test":
        return cmd_self_test(config, smartctl, msmtp, "long")
    elif args.command == "status":
        return cmd_status(config, smartctl)

    return 0


if __name__ == "__main__":
    sys.exit(main())

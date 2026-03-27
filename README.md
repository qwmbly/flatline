# Flatline

SMART drive monitoring wrapper for [smartmontools](https://www.smartmontools.org/). Monitors drive health in the background and sends email alerts via [msmtp](https://marlam.de/msmtp/) when something needs attention.

Built for home servers and NAS boxes. Zero pip dependencies, Python 3.11+ stdlib only.

## What it monitors

- SMART health status (pass/fail)
- Reallocated sectors, pending sectors, uncorrectable errors (alerts on any increase)
- Drive temperature (alerts above configurable threshold)
- Self-test results
- Drives disappearing from scan (dropped off the bus)
- NVMe media errors

## How it works

Flatline runs on a schedule via systemd timers:

| Timer | Frequency | What it does |
|---|---|---|
| `flatline-check` | Hourly | Polls SMART data, compares against previous state, alerts on changes |
| `flatline-short-test` | Weekly | Triggers short self-tests on all drives |
| `flatline-long-test` | Monthly | Triggers long self-tests on all drives |

State is tracked by drive serial number (not device path), so `/dev/sdX` reassignment across reboots won't cause false alerts. Historical readings are stored in SQLite for trend analysis.

## Requirements

- Python 3.11+
- smartmontools
- msmtp (configured with your SMTP relay)

## Install

```bash
git clone https://github.com/qwmbly/flatline.git
cd flatline
sudo ./install.sh
```

The install script validates dependencies, copies files to `/opt/flatline/`, and enables the systemd timers.

Then edit the config:

```bash
sudo nano /opt/flatline/config.toml
```

At minimum, set your email address under `[email]`.

## Usage

```
sudo /opt/flatline/flatline.py [-c CONFIG] [-v] COMMAND
```

| Command | Description |
|---|---|
| `check` | Run a health check (normally run by the hourly timer) |
| `short-test` | Trigger short self-tests, then check |
| `long-test` | Trigger long self-tests, then check |
| `status` | Print a human-readable summary of all drives |

## Configuration

See [config.example.toml](config.example.toml) for all options. Defaults are sane; the only required setting is `email.to`.

## License

MIT

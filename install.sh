#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/flatline"
SYSTEMD_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[x]${NC} $1"; }

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (or with sudo)."
    exit 1
fi

info "Checking dependencies..."

missing=()
command -v smartctl >/dev/null 2>&1 || missing+=("smartmontools")
command -v msmtp    >/dev/null 2>&1 || missing+=("msmtp")
command -v python3  >/dev/null 2>&1 || missing+=("python3")

if [[ ${#missing[@]} -gt 0 ]]; then
    error "Missing packages: ${missing[*]}"
    echo "  Install with: apt install ${missing[*]}"
    exit 1
fi

# Check Python version (need 3.11+ for tomllib)
py_version=$(python3 -c 'import sys; print(f"{sys.version_info.minor}")')
if [[ "$py_version" -lt 11 ]]; then
    error "Python 3.11+ required (found 3.${py_version}). tomllib is not available in older versions."
    exit 1
fi

info "All dependencies satisfied."

# ---------------------------------------------------------------------------
# Install files
# ---------------------------------------------------------------------------

info "Creating ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"

info "Copying files to ${INSTALL_DIR}..."
cp "${SCRIPT_DIR}/flatline.py" "${INSTALL_DIR}/flatline.py"
chmod 755 "${INSTALL_DIR}/flatline.py"

if [[ ! -f "${INSTALL_DIR}/config.toml" ]]; then
    cp "${SCRIPT_DIR}/config.example.toml" "${INSTALL_DIR}/config.toml"
    warn "Edit ${INSTALL_DIR}/config.toml to set your email address before enabling timers."
else
    info "Config already exists at ${INSTALL_DIR}/config.toml, leaving it in place."
fi

# ---------------------------------------------------------------------------
# systemd units
# ---------------------------------------------------------------------------

info "Installing systemd units..."
for unit in "${SCRIPT_DIR}"/systemd/*; do
    cp "$unit" "${SYSTEMD_DIR}/"
done

systemctl daemon-reload

info "Enabling timers..."
systemctl enable flatline-check.timer
systemctl enable flatline-short-test.timer
systemctl enable flatline-long-test.timer

systemctl start flatline-check.timer
systemctl start flatline-short-test.timer
systemctl start flatline-long-test.timer

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

info "Verifying smartctl can scan drives..."
if smartctl --scan >/dev/null 2>&1; then
    drive_count=$(smartctl --scan | wc -l)
    info "Found ${drive_count} drive(s)."
else
    warn "smartctl --scan failed. You may need to check permissions or device access."
fi

echo ""
info "Installation complete."
info "Active timers:"
systemctl list-timers 'flatline-*' --no-pager
echo ""
warn "Next steps:"
echo "  1. Edit ${INSTALL_DIR}/config.toml (set your email address)"
echo "  2. Configure msmtp (~/.msmtprc or /etc/msmtprc) with your mail provider"
echo "  3. Test with: sudo /opt/flatline/flatline.py status"
echo "  4. Test alerting with: sudo /opt/flatline/flatline.py check"

#!/usr/bin/env bash
# Install a per-user systemd service for TESTING on a Linux workstation
# (Ubuntu, Debian, Fedora, ...). No root required, no pip/venv required.
#
#   ./deploy/install-dev.sh                 # offline simulation, no keys/network
#   ./deploy/install-dev.sh --paper         # Alpaca paper account + live data
#   ./deploy/install-dev.sh --enable        # also enable + start the service
#
# Afterwards:
#   systemctl --user status krabs
#   journalctl --user -u krabs -f
#   ./krabs status -c configs/offline.toml
#
# For an always-on machine, allow the user service to run before login:
#   loginctl enable-linger "$USER"
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/krabs"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
UNIT="${UNIT_DIR}/krabs.service"
MODE="offline"
CONFIG="${REPO}/configs/offline.toml"
ENABLE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --paper)  MODE="paper"; CONFIG="${REPO}/configs/alpaca-paper.toml" ;;
    --config) MODE="custom"; CONFIG="$(realpath "$2")"; shift ;;
    --enable) ENABLE=1 ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ ! -x "${REPO}/krabs" ]]; then
  chmod +x "${REPO}/krabs"
fi
if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "systemd --user is not available in this session" >&2
  exit 1
fi

mkdir -p "${UNIT_DIR}" "${CONF_DIR}" "${REPO}/var" "${REPO}/reports"

if [[ "${MODE}" == "offline" ]]; then
  EXEC="${REPO}/krabs paper --offline -c ${CONFIG}"
  DESCRIPTION="Krabs offline deployment test (synthetic bars, no broker)"
  ENV_FILE=""
else
  if [[ "${MODE}" == "paper" ]]; then
    CONF_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/krabs"
    if [[ ! -f "${CONF_DIR}/krabs.env" ]]; then
      install -m 0600 "${REPO}/deploy/krabs.env.example" "${CONF_DIR}/krabs.env"
      echo "created ${CONF_DIR}/krabs.env -- add ALPACA_API_KEY / ALPACA_SECRET_KEY"
      echo "then restart: systemctl --user restart krabs"
    fi
  fi
  EXEC="${REPO}/krabs paper -c ${CONFIG}"
  DESCRIPTION="Krabs Alpaca paper trading (live data)"
  ENV_FILE="EnvironmentFile=-${CONF_DIR}/krabs.env"
fi

cat > "${UNIT}" <<EOF
[Unit]
Description=${DESCRIPTION}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO}
Environment=PYTHONPATH=${REPO}/src
Environment=PYTHONUNBUFFERED=1
${ENV_FILE}
ExecStart=${EXEC}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
echo "installed ${UNIT}"
echo "  mode    : ${MODE}"
echo "  config  : ${CONFIG}"
echo "  command : ${EXEC}"

if [[ "${ENABLE}" -eq 1 ]]; then
  systemctl --user enable --now krabs.service
  sleep 1
  systemctl --user --no-pager status krabs.service || true
  echo
  echo "logs: journalctl --user -u krabs -f"
else
  cat <<EOF

Start it with:
  systemctl --user enable --now krabs.service

Follow logs:
  journalctl --user -u krabs -f

Check the heartbeat:
  ${REPO}/krabs status -c ${CONFIG}

Stop trading (keeps the service running):
  ${REPO}/krabs kill -c ${CONFIG}

Stop the service:
  systemctl --user disable --now krabs.service
EOF
fi

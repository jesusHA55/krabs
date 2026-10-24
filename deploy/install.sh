#!/usr/bin/env bash
# One-time Raspberry Pi installation. Run as root from the repository root:
#
#   sudo ./deploy/install.sh
#   sudo ./deploy/install.sh --rust     # also build the optional Rust core
#
# Creates the `krabs` system user, installs into /opt/krabs/.venv, copies the
# systemd units, and leaves services DISABLED so you can review config first.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root: sudo $0" >&2
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/opt/krabs
CONF=/etc/krabs
BUILD_RUST=0
[[ "${1:-}" == "--rust" ]] && BUILD_RUST=1

echo "==> user and directories"
if ! id krabs >/dev/null 2>&1; then
  useradd --system --home-dir "${DEST}" --shell /usr/sbin/nologin krabs
fi
install -d -o krabs -g krabs -m 0750 "${DEST}" "${DEST}/var" "${DEST}/reports" "${DEST}/backups"
install -d -o root -g krabs -m 0750 "${CONF}"

echo "==> application files"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '.git' --exclude 'var' --exclude 'reports' --exclude 'backups' \
    --exclude '.venv' --exclude '__pycache__' \
    "${SRC}/" "${DEST}/"
else
  cp -a "${SRC}/." "${DEST}/"
  rm -rf "${DEST}/.git" "${DEST}/var" "${DEST}/reports" "${DEST}/backups"
fi
chown -R krabs:krabs "${DEST}"
chmod 0755 "${DEST}/krabs" "${DEST}/scripts/"*.sh

echo "==> python environment"
python3 -m venv "${DEST}/.venv"
"${DEST}/.venv/bin/pip" install --upgrade pip
"${DEST}/.venv/bin/pip" install "${DEST}"
chown -R krabs:krabs "${DEST}/.venv"

if [[ "${BUILD_RUST}" -eq 1 ]]; then
  echo "==> rust hot path"
  "${DEST}/.venv/bin/pip" install maturin
  if command -v runuser >/dev/null 2>&1; then
    ( cd "${DEST}" && runuser -u krabs -- env RUSTFLAGS="-C target-cpu=native" \
        "${DEST}/.venv/bin/maturin" develop --release \
        --manifest-path crates/krabs_core/Cargo.toml )
  else
    ( cd "${DEST}" && sudo -u krabs env RUSTFLAGS="-C target-cpu=native" \
        "${DEST}/.venv/bin/maturin" develop --release \
        --manifest-path crates/krabs_core/Cargo.toml )
  fi
fi

echo "==> configuration"
if [[ ! -f "${CONF}/krabs.toml" ]]; then
  install -o root -g krabs -m 0640 "${DEST}/configs/alpaca-paper.toml" "${CONF}/krabs.toml"
  echo "    created ${CONF}/krabs.toml (paper mode)"
fi
if [[ ! -f "${CONF}/krabs.env" ]]; then
  install -o root -g krabs -m 0600 "${DEST}/deploy/krabs.env.example" "${CONF}/krabs.env"
  echo "    created ${CONF}/krabs.env -- add your keys now"
fi

echo "==> systemd units"
install -m 0644 "${DEST}/deploy/systemd/krabs.service" /etc/systemd/system/krabs.service
install -m 0644 "${DEST}/deploy/systemd/krabs-dashboard.service" /etc/systemd/system/krabs-dashboard.service
install -m 0644 "${DEST}/deploy/systemd/krabs-backup.service" /etc/systemd/system/krabs-backup.service
install -m 0644 "${DEST}/deploy/systemd/krabs-backup.timer" /etc/systemd/system/krabs-backup.timer
systemctl daemon-reload

cat <<EOF

Installed. Next steps:

  1. sudo nano ${CONF}/krabs.toml     # symbols, limits, mode=paper
  2. sudo nano ${CONF}/krabs.env      # ALPACA_API_KEY / ALPACA_SECRET_KEY
  3. ${DEST}/.venv/bin/krabs doctor -c ${CONF}/krabs.toml
  4. sudo systemctl enable --now krabs.service krabs-backup.timer
  5. sudo systemctl enable --now krabs-dashboard.service   # read-only, 127.0.0.1:8787
  6. journalctl -u krabs -f
  7. sudo -u krabs cat /opt/krabs/var/dashboard.token

Read /opt/krabs/docs/deployment.md and operations.md before switching to live.
EOF

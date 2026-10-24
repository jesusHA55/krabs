# Deployment on a Raspberry Pi

Target: a headless Pi that boots, reconnects, and trades (or refuses to trade)
without anyone touching it.

---

## 1. Bill of materials

| Item | Recommended | Notes |
|---|---|---|
| Board | Raspberry Pi 5, 8 GB (Pi 4 4 GB minimum) | Live trading needs ~300 MB; 8 GB is for research/backtests. |
| Storage | NVMe SSD + HAT, or USB 3 SSD (≥64 GB) | **Do not run from an SD card.** SQLite commits will wear it out. |
| Power | Official PSU + UPS (≥5 V/5 A for Pi 5) | Brownouts corrupt databases and corrupt trust. |
| Cooling | Active cooler | Sustained backtests throttle an uncooled Pi 4 in minutes. |
| Network | Ethernet preferred | Wi-Fi with Tailscale as fallback. |
| Case | Vented | Heat is the only thing that reliably kills a Pi. |

---

## 2. Operating system

Install **Raspberry Pi OS Lite 64-bit** (Bookworm or newer) and boot from SSD.
In `raspi-config`: set hostname, locale, timezone **UTC**, enable SSH, disable
desktop/auto-login.

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3 python3-venv python3-dev git sqlite3 chrony rsync \
                    zram-tools unattended-upgrades
sudo timedatectl set-ntp true
timedatectl status                 # "System clock synchronized: yes"
```

Mount the SSD with `noatime` in `/etc/fstab`. If you keep logs on disk, cap
journald (`/etc/systemd/journald.conf`: `SystemMaxUse=500M`).

Optional: `sudo apt install rpi-eeprom && sudo rpi-eeprom-config --edit` to set
`BOOT_ORDER=0xf416` (NVMe/USB before SD) and a power-off threshold.

---

## 3. Install Krabs

```bash
git clone <repo> ~/Krabs && cd ~/Krabs
sudo ./deploy/install.sh            # add --rust to build the native core
```

The installer:

- creates the unprivileged `krabs` system user;
- copies the app to `/opt/krabs` (var/reports/backups excluded);
- builds `/opt/krabs/.venv` and installs the `krabs` console script;
- creates `/etc/krabs/krabs.toml` (Alpaca **paper** template);
- creates `/etc/krabs/krabs.env` with mode `0600`;
- installs `krabs.service`, `krabs-backup.service`, `krabs-backup.timer`;
- leaves everything **disabled** until you review it.

### Rust hot path (optional)

```bash
sudo -u krabs /opt/krabs/.venv/bin/pip install maturin
cd /opt/krabs
sudo -u krabs RUSTFLAGS="-C target-cpu=native" \
  /opt/krabs/.venv/bin/maturin develop --release \
  --manifest-path crates/krabs_core/Cargo.toml
```

`target-cpu=native` is correct for a machine that never moves. Verify:

```bash
/opt/krabs/.venv/bin/krabs doctor -c /etc/krabs/krabs.toml   # backend: rust
```

Skipping Rust is supported; `doctor` warns and the Python fallback runs.

---

## 3b. Ubuntu / Linux workstation (test deployment)

No root, no pip and no virtualenv are required: `./krabs` runs from the checkout.

```bash
# one-off
./krabs backtest -c configs/example.toml
./krabs paper --offline -c configs/offline.toml     # full runtime, synthetic bars

# per-user systemd service (starts at login, survives logout with linger)
./deploy/install-dev.sh --enable
journalctl --user -u krabs -f
./krabs status -c configs/offline.toml
./krabs kill   -c configs/offline.toml
systemctl --user disable --now krabs.service
```

`--offline` is deliberately loud: it prints that no broker is connected and
labels the run `paper-sim`. It is a deployment/plumbing test, not a research
tool and definitely not a profitability check.

For real Alpaca **paper** trading:

```bash
./deploy/install-dev.sh --paper --enable
# the first run creates ~/.config/krabs/krabs.env; add keys and:
systemctl --user restart krabs
journalctl --user -u krabs -f
```

The generated unit lives at `~/.config/systemd/user/krabs.service` and uses the
repo in place. Keep the checkout where it is; if you move it, re-run the
installer. For a headless workstation run `loginctl enable-linger "$USER"` so
the user manager starts the service before login.

If you prefer a conventional root install with `/opt/krabs`, a venv and the
hardened `krabs.service`, the Raspberry Pi installer below works on Ubuntu too.

## 4. Secrets

```bash
sudo nano /etc/krabs/krabs.env      # ALPACA_API_KEY / ALPACA_SECRET_KEY
sudo chown root:krabs /etc/krabs/krabs.env
sudo chmod 0600 /etc/krabs/krabs.env
```

Rules:

- Paper keys while testing; live keys only when you are ready to lose money.
- Restrict the key to trading; disable funding/withdrawals.
- IP-allowlist the Pi's egress address if Alpaca supports it for your account.
- Never put keys in `krabs.toml`, git, shell history, reports or chat.
- `EnvironmentFile` is read by systemd as root and passed to the service; the
  file is not readable by the `krabs` user directly.

Use `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` names, or change
`[broker].api_key_env` / `api_secret_env` in the config.

---

## 5. Configuration

Start from `/etc/krabs/krabs.toml` (a copy of `configs/alpaca-paper.toml`).

Recommended first profile:

```toml
[app]
mode = "paper"
log_json = true

[market]
symbols = ["SPY"]
timeframe = "5Min"

[risk]
max_order_notional = 500.0
max_position_pct = 0.20
max_gross_exposure_pct = 0.50
max_daily_loss_pct = 0.02
max_open_orders = 5
duplicate_window_s = 120

[feed]
provider = "alpaca"
poll_seconds = 15.0
```

`poll_seconds` should be shorter than the bar interval. For `5Min`, 15–30 s is
plenty. Do not poll faster than the Alpaca plan allows.

Validate without trading:

```bash
/opt/krabs/.venv/bin/krabs doctor -c /etc/krabs/krabs.toml
/opt/krabs/.venv/bin/krabs backtest -c configs/example.toml    # offline sanity
```

`doctor` must show no FAILs. Warnings for temperature, NTP or missing heartbeat
are acceptable only before the first start.

---

## 6. systemd

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now krabs-backup.timer
sudo systemctl enable --now krabs.service
systemctl status krabs --no-pager
journalctl -u krabs -f
```

Change the deployed mode by editing `ExecStart` in
`/etc/systemd/system/krabs.service`:

```
ExecStart=/opt/krabs/.venv/bin/krabs paper -c /etc/krabs/krabs.toml
ExecStart=/opt/krabs/.venv/bin/krabs broker-paper -c /etc/krabs/krabs.toml
ExecStart=/opt/krabs/.venv/bin/krabs live -c /etc/krabs/krabs.toml --i-understand-the-risk
```

The unit already hardens the service: separate user, read-only filesystem,
private /tmp, no new privileges, restricted syscalls, 1 GB memory cap, 2 CPU
quota, writable paths only `var/` and `reports/`.

Enable the read-only dashboard (loopback; use Tailscale/SSH for remote access):

```bash
sudo systemctl enable --now krabs-dashboard.service
sudo -u krabs cat /opt/krabs/var/dashboard.token
# open http://127.0.0.1:8787/ on the Pi, or tunnel:
ssh -N -L 8787:127.0.0.1:8787 pi@your-pi
```

To allow the kill switch in the UI, add `--allow-controls` to the dashboard
unit's `ExecStart` (`systemctl edit krabs-dashboard`). See `docs/dashboard.md`.

Useful operations:

```bash
sudo systemctl restart krabs          # startup reconciliation runs again
sudo systemctl stop krabs             # SIGTERM: graceful, cancels open orders
sudo kill -USR1 "$(systemctl show -p MainPID --value krabs)"   # kill switch
```

---

## 7. Monitoring

Minimum viable monitoring, no extra services:

1. **systemd** restarts on failure; `systemctl status` shows the last exit.
2. **Heartbeat**: `var/heartbeat.json` is rewritten every cycle (atomic replace).
   A cron/watchdog job should alert if `ts` is older than `3 × poll_seconds`.
3. **Halt state**: `heartbeat.halted` or `krabs status`.
4. **Errors**: journald `grep -i error`; the runtime counts errors in the heartbeat.
5. **Disk/temperature**: `krabs doctor`.

Example watchdog (run from root cron every 5 minutes):

```bash
# /etc/cron.d/krabs-watchdog
*/5 * * * * root /opt/krabs/.venv/bin/krabs doctor -c /etc/krabs/krabs.toml \
  | grep -E '^\[FAIL\]' && systemctl restart krabs
```

Be careful with automatic restarts on FAIL: a missing credential or a disk
failure will restart-loop. Prefer alerting for those; restart only on heartbeat
staleness.

For richer monitoring, scrape `heartbeat.json` and expose it via node_exporter's
textfile collector. No code changes required.

Optional external alert (ntfy, Telegram, email) can be a one-line cron that
curls when `halted` is true. Keep alerting outside the trading process.

---

## 8. Backups

The timer runs nightly at 23:30 US/Eastern:

```
/opt/krabs/backups/krabs-YYYYmmddTHHMMSSZ.db.gz
```

It uses SQLite's online backup API and keeps 14 files. Test a restore:

```bash
gunzip -c /opt/krabs/backups/krabs-*.db.gz > /tmp/restore.db
sqlite3 /tmp/restore.db "select run_id, mode, strategy, status, final_equity from runs order by started_at desc limit 5;"
sqlite3 /tmp/restore.db "select count(*) from fills;"
```

Copy backups off the Pi (rsync to a NAS, `rclone`, or Tailscale to a laptop).
A backup that only exists on the device that failed is not a backup.

Reports can be regenerated from a backtest; the database is the irreplaceable
part.

---

## 9. Updates

```bash
sudo systemctl stop krabs
cd /opt/krabs && sudo -u krabs git pull        # or re-run deploy/install.sh
sudo -u krabs .venv/bin/pip install --upgrade .
sudo -u krabs .venv/bin/krabs doctor -c /etc/krabs/krabs.toml
sudo systemctl start krabs
```

Run `krabs kill` before updating during market hours. Startup reconciliation
will cancel orphans, but a clean stop is better.

Pin `unattended-upgrades` to security-only and schedule reboots outside market
hours (`/etc/apt/apt.conf.d/50unattended-upgrades` and
`/etc/apt/apt.conf.d/20auto-upgrades`). A surprise reboot mid-session is
survivable (reconciliation) but avoidable.

---

## 10. Hardening checklist

- [ ] SSH keys only, password auth disabled, root login disabled.
- [ ] Tailscale (or WireGuard) for remote access; no forwarded ports.
- [ ] Firewall default-deny inbound (`ufw`).
- [ ] `krabs` runs as an unprivileged system user; no sudo rights.
- [ ] `/etc/krabs/krabs.env` is `0600 root:krabs`.
- [ ] API keys are trading-only and paper keys are used until promotion.
- [ ] Kernel/firmware updates scheduled off-hours.
- [ ] UPS connected and tested (pull power, confirm clean shutdown).
- [ ] Heartbeat watchdog + external alert tested (stop the service, get an alert).
- [ ] Backup restore tested.
- [ ] `krabs doctor` has zero FAILs.
- [ ] Kill switch tested from SSH and from `SIGUSR1`.
- [ ] `[risk]` limits reviewed against account size (max order ≤ 10% of equity).

---

## 11. First-week runbook

| Day | Action |
|---|---|
| 1 | Install, `doctor`, run `paper` for a full session. Confirm heartbeat and bars. |
| 2–5 | Paper mode. Compare fills/PnL with the backtest after costs. |
| 6–10 | `broker-paper` with small limits. Watch reconciliations and rejects. |
| 11 | Review the audit DB: `runs`, `rejects`, halt events. |
| 12 | Live with `max_order_notional` ≈ 1–2% of equity and a 1% daily stop. |
| 13+ | Increase limits only after a clean week; never remove the daily stop. |

If anything is ambiguous, stop trading. Flat and alive beats clever and blown
up.

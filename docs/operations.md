# Operations runbook

Everything here assumes the systemd install from `docs/deployment.md` and
`KRABS=/opt/krabs/.venv/bin/krabs`, `CONF=/etc/krabs/krabs.toml`.

---

## Daily checks (2 minutes)

```bash
$KRABS status -c $CONF
# or open the dashboard (see docs/dashboard.md)
$KRABS doctor -c $CONF
journalctl -u krabs --since "24 hours ago" -p warning --no-pager
```

Look for:

- heartbeat age < 3 × `poll_seconds`;
- `halted: false`;
- equity and positions match the broker UI;
- no repeating `risk reject` or `broker error` lines;
- kill switch cleared.

---

## Stop trading now

```bash
$KRABS kill -c $CONF                 # blocks new orders, cancels working orders
$KRABS kill -c $CONF --clear         # resume only after understanding the cause
$KRABS flatten -c $CONF --yes        # optional: liquidate everything (Alpaca)
```

`kill -USR1 <pid>` does the same as `krabs kill`. The runtime keeps running so
you retain heartbeats and logs; it simply refuses to send orders.

The kill switch is a **file**. It survives restarts, crashes and reboots. No
code path deletes it automatically.

---

## Startup / restart behaviour

On every start in `broker-paper`/`live`:

1. Open audit run.
2. Fetch open orders → **cancel them all** (orphans from a dead process).
3. Fetch account + positions → `BrokerWallet.refresh`.
4. Seed filled-quantity watermarks for this run's client ids.
5. Warm indicators from `history_days` of bars.
6. Begin the loop.

Safe to restart at any time. If step 2 or 3 fails, the process exits and
systemd retries; it will not trade blind.

---

## Incident playbook

### Feed stalled / stale data

Symptom: heartbeat `halted: true`, reason `stale market data`, no bars for
`max_data_age_s`.

1. `systemctl status krabs`, check network: `curl -sI https://data.alpaca.markets`.
2. Check Alpaca status page / your data subscription.
3. Confirm the config timeframe and `poll_seconds` are sane.
4. Once data flows, the halt clears automatically on the next cycle.
5. If it recurs daily at the same time, your calendar/timezone is wrong.

### Daily loss halt

Symptom: reason `daily loss limit`.

This is **sticky for the session by design**. Review the fills and the market
regime. Do not restart to "reset" the limit unless you have deliberately decided
to override risk (and accept the consequences). Next session clears it.

### Broker outage / repeated 5xx

The client retries with backoff. After 20 failed cycles the runtime halts.
Positions remain at the broker; local state may lag until sync succeeds.
Do not panic-flatten into an outage unless the strategy requires it.

### Orders rejected repeatedly

Read the reason in `journalctl` and in the `events` table:

- `insufficient buying power` → limits too tight or positions not settled;
- `short selling disabled` → strategy is trying to reverse a position;
- `price collar` → stale last price or a bad limit price;
- `duplicate intent` → strategy is firing every bar; fix the strategy state;
- `403`/`422` from Alpaca → market closed, symbol halted, or invalid qty.

### Process crash / reboot

Just restart. Startup cancels orphans and reconciles. Verify:
`$KRABS status`, broker UI positions, `sqlite3 var/krabs.db "select * from runs order by started_at desc limit 1;"`.

### Disk full

SQLite writes fail loudly and the runtime treats the exception as a halt.
Free space (`journalctl --vacuum-size=200M`, rotate reports), verify DB integrity:

```bash
sqlite3 /opt/krabs/var/krabs.db "PRAGMA integrity_check;"
```

### Thermal throttling

`vcgencmd measure_temp`; `cat /sys/class/thermal/thermal_zone0/temp`.
Above 80 °C, improve cooling and move backtests off-hours. `doctor` fails at
85 °C. Throttling itself does not corrupt state, but it can delay cycles.

### Kill switch engaged unexpectedly

`cat var/KILL` shows timestamp + reason. `SIGUSR1` means someone/something sent
it. Investigate before `--clear`.

---

## Weekly maintenance

```bash
# audit summary
sqlite3 /opt/krabs/var/krabs.db \
  "select run_id, mode, strategy, status, initial_cash, final_equity from runs order by started_at desc limit 10;"

# rejection reasons
sqlite3 /opt/krabs/var/krabs.db \
  "select json_extract(payload,'$.reason') r, count(*) from events where topic='risk.reject' group by r order by 2 desc;"

# halts
sqlite3 /opt/krabs/var/krabs.db \
  "select ts, json_extract(payload,'$.reason') from events where topic='risk' order by ts desc limit 20;"

# backups
ls -lh /opt/krabs/backups | tail
```

Check `df -h`, `vcgencmd measure_temp`, `timedatectl`, and whether the strategy's
behaviour still matches the paper-period baseline.

---

## Restoring from backup

```bash
sudo systemctl stop krabs
gunzip -c /opt/krabs/backups/krabs-YYYY....db.gz > /tmp/restore.db
sqlite3 /tmp/restore.db "PRAGMA integrity_check;"      # expect: ok
sudo install -o krabs -g krabs -m 0640 /tmp/restore.db /opt/krabs/var/krabs.db
sudo rm -f /opt/krabs/var/krabs.db-wal /opt/krabs/var/krabs.db-shm
sudo systemctl start krabs
```

Restoring the audit trail does **not** restore positions. The broker is the
source of truth for account state; reconciliation adopts it on startup.

---

## Emergency: flatten and stop

```bash
$KRABS kill -c $CONF
$KRABS flatten -c $CONF --yes
sudo systemctl stop krabs
sudo systemctl disable krabs        # stay down until the post-mortem is done
```

Then: export the last 3 backup files, the journal since the last good day, and
the `runs`/`events` rows for the affected run.

---

## Change management

- One change at a time: strategy, parameters, or limits — never all three.
- Record the config hash in the commit message or a `CHANGELOG`.
- Paper-trade every strategy change for at least a week.
- Risk limit changes are production changes; review them twice.
- Keep the previous config and a known-good backup before every live change.

---

## On-call checklist (print this)

```
[ ] Is the kill switch engaged?          cat /opt/krabs/var/KILL
[ ] Is the process alive?                systemctl status krabs
[ ] Is the heartbeat fresh?              krabs status
[ ] Is it halted, and why?               heartbeat.json -> halt_reason
[ ] Do positions match the broker?       broker UI vs heartbeat.positions
[ ] Any repeated rejects/errors?         journalctl -u krabs -p warning
[ ] Is the clock synced?                 timedatectl
[ ] Is the disk/temperature sane?        df -h; vcgencmd measure_temp
[ ] Do you understand the cause?         if not: stop trading
```

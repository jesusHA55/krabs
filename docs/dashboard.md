# Dashboard

A mobile-friendly, dependency-free web dashboard for monitoring Krabs from a
phone or laptop. It is a **separate process** from the trading bot: it reads the
heartbeat, SQLite audit trail, kill switch and reports, so it still works if the
bot is stopped or crashed. A dashboard bug cannot affect execution.

```
┌──────────────┐   heartbeat.json   ┌──────────────┐
│ krabs paper/ │ ─────────────────► │  dashboard   │ ──► browser / phone
│ live runtime │   SQLite (WAL)     │  (read-only) │
└──────────────┘   reports/, KILL   └──────────────┘
```

## Start it

```bash
./krabs dashboard -c configs/offline.toml
# or, with the kill switch enabled:
./krabs dashboard -c configs/alpaca-paper.toml --allow-controls
```

Defaults: `127.0.0.1:8787`, token authentication, read-only. The token is
generated on first run:

```bash
cat var/dashboard.token
```

Open <http://127.0.0.1:8787/>, enter the token, and the session cookie keeps you
logged in for seven days. Print the token once at startup with `--show-token` if
you prefer.

Useful flags:

| Flag | Meaning |
|---|---|
| `--host 127.0.0.1` | Bind address. Do not expose plain HTTP to the internet. |
| `--port 8787` | Port. |
| `--token-file PATH` | Token location (default `var/dashboard.token`). |
| `--token TOKEN` | Use an explicit token instead of the file. |
| `--no-auth` | Disable auth; **loopback only**, for local development. |
| `--allow-controls` | Enable engage/clear kill switch from the UI. |
| `--reports-dir DIR` | Browse a different report directory. |

Add `?refresh=10` to the URL to change the auto-refresh interval (1–60 s,
default 5 s).

## What it shows

| Tab | Contents |
|---|---|
| **Overview** | Equity, total/day P&L, cash, buying power, exposure, heartbeat age, status, positions preview, session details, health, warnings |
| **Pearl** | Model readiness, signals and per-node values from `strategy_diagnostics` |
| **Positions** | Symbol, quantity, average, last, value, unrealized P&L, portfolio weight |
| **Orders** | Latest 50 orders with status, fill quantity, average price and rejection reason |
| **Fills** | Latest 50 executions with notional and commission |
| **Runs** | Audit trail: mode, strategy, status, initial/final equity, return |
| **Reports** | Backtests, optimizations and sweeps, with key metrics and a raw-JSON link |
| **Risk** | Every configured limit plus the execution model |
| **Strategies** | Registered strategies, descriptions and default parameters |

The Pearl tab appears when the running strategy is a `PearlStrategy` and the
runtime heartbeat contains `strategy_diagnostics` (readiness, signals and node
values). The kill switch panel is on **Overview** and **Risk**. The dashboard never places
orders, flattens positions or edits configuration; those stay in the CLI.

## Remote access from a phone

The safe pattern is: keep the dashboard bound to `127.0.0.1` and put an
authenticated TLS tunnel in front of it.

### Tailscale (recommended)

```bash
# on the Pi / server
sudo tailscale up
tailscale serve --bg 8787          # HTTPS on your tailnet, localhost only
tailscale serve status
```

Open `https://<machine>.<tailnet>.ts.net/` from your phone. The dashboard token
is still required. Check `tailscale serve --help` for your version's syntax.

### SSH tunnel

```bash
ssh -N -L 8787:127.0.0.1:8787 user@your-server
# then open http://127.0.0.1:8787/ on the laptop
```

On iOS/Android, an SSH client with local forwarding works the same way.

### Reverse proxy with TLS

Caddy example:

```caddy
krabs.example.com {
    reverse_proxy 127.0.0.1:8787
}
```

Do **not** port-forward `0.0.0.0:8787` to the public internet. The token is a
secret, but the connection is plain HTTP without a proxy.

## systemd

The repository ships `deploy/systemd/krabs-dashboard.service` for the
`/opt/krabs` install:

```bash
sudo systemctl enable --now krabs-dashboard.service
sudo journalctl -u krabs-dashboard -f
```

It binds to `127.0.0.1:8787`, runs as the `krabs` user and is read-only. To enable
the kill switch, add `--allow-controls` to `ExecStart` and restart:

```bash
sudo systemctl edit krabs-dashboard.service
```

Read the token:

```bash
sudo -u krabs cat /opt/krabs/var/dashboard.token
```

For a per-user install on a workstation, run it in the foreground or create a
user unit pointing at `./krabs dashboard -c ...` (same pattern as
`deploy/install-dev.sh`).

## API

Every view is backed by a JSON endpoint, useful for scripts, Stream Deck
shortcuts or monitoring:

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /api/snapshot` | Bearer or session | Everything the UI renders |
| `GET /api/health` | Bearer or session | Small health payload for monitors |
| `GET /api/report?path=...` | Bearer or session | One report JSON, restricted to the reports dir |
| `POST /api/kill` | Bearer or session + CSRF + `--allow-controls` | `{"action":"engage"|"clear","reason":"..."}` |

```bash
TOKEN=$(cat var/dashboard.token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/api/health
```

## Security model

- Random 256-bit token, constant-time comparison, login rate limiting.
- Session cookie is `HttpOnly` + `SameSite=Strict`; mutations require a
  double-submit CSRF token.
- Strict CSP, no inline scripts, no external CDNs, no CORS headers.
- Static files are whitelisted; report paths cannot escape the reports directory.
- Read-only unless `--allow-controls`; the only mutation is the kill file.
- Bind to loopback and use Tailscale/Caddy for TLS. The process needs read access
  to `var/`, `reports/` and write access to `var/` for the token and kill file.

## Limitations

- Positions come from the heartbeat, not a position history table. If the bot is
  not running, the last known positions are shown until the heartbeat is
  replaced; a backtest-only database has no positions.
- Reports are cached for five seconds; a large `reports/` directory shows the 50
  newest entries.
- One dashboard serves one config. Run another instance on a different port for
  another account/strategy.
- No service control (start/stop/restart), no order entry, no flatten. Use
  `krabs`/systemd for those.

# ZARIB.

Independent per-inbound traffic multipliers for MHSanaei/3x-ui, with a monochrome Persian RTL dashboard.

![Dashboard](docs/preview-desktop.png)

## One-command installation

Run as root on the server already running your native 3x-ui panel and Xray:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/farstar-team/zarib-3x-ui/main/install.sh)
```

The installer discovers the panel settings, database, hostname and core management port. It prints the companion URL; the first free port among 2087, 2083 and 2054 is selected. Sign into 3x-ui using the same hostname and browser, then open that URL. There is no panel-backlink in the dashboard.

Re-run the same command to update. Saved multipliers and accounting state are retained. Only the companion service restarts; the installer does not restart 3x-ui or Xray.

Optional flags include `--host panel.example.com`, `--web-port 2087`, `--panel-db /path/to/x-ui.db` and `--check`. Run `python3 installer.py --help` for the full list. Shell bootstrap may install missing system packages even for `--check`; Python compatibility checking does not change panel files, factors or services.

## Accounting

Version 1.1.1 preserves the cumulative counter's already weighted value when a factor is edited. Previously collected usage is never repriced at the new factor. Per-counter boundaries are persisted and tied to the configured core process generation; legacy reset-based collection is supported too.

Each inbound has its own factor, while the panel keeps the account's shared quota. Downloading 2 GB on a 1.5× inbound deducts 3 GB; 2 GB on a 1× inbound deducts 2 GB; 1 GB on a 5× inbound deducts 5 GB. Both upload and download count. Physical inbound/server traffic remains unscaled.

Fresh installations start every factor at 1. Factors range from 0 to 10 with up to three decimal places. Zero makes new traffic free; it does not reactivate an expired or disabled account. Historical usage, quota, expiration, UUID and flow remain unchanged.

The first non-one factor takes approximately five seconds to prepare and applies to newly authenticated connections. Existing legacy connections continue at factor 1 until they reconnect naturally. Later changes affect the next collection of uncollected traffic, not an exact packet-time boundary. Fractional byte carry is persisted to prevent repeated rounding loss.

## Supported layout

- Native systemd Debian 12+/Ubuntu 22.04+; Python 3.10+.
- Existing VLESS inbounds and a loopback IPv4 Xray API exposing HandlerService, StatsService and GetInboundUsers.
- SQLite read-only catalog, or local PostgreSQL with a dedicated column-restricted reader role.
- Existing panel TLS certificate reused for HTTPS; HTTP panels get HTTP. Certificate issuance is not included.
- Docker/remote PostgreSQL and unusual reverse-proxy layouts need separate configuration.
- Email-based routing rules need a separate compatibility review; factor edits are blocked when detected.

Verified against 3x-ui 3.11.0 and Xray 26.10.21. Upstream executables and schemas are not patched. Ordinary upstream updates do not replace the extension's directories, but future incompatible API/schema changes can require an extension update.

## State and operations

The compatibility-preserving service name is `farstar-inbound-multiplier.service`. Code lives in `/opt/farstar-inbound-multiplier`, private configuration in `/etc/farstar-inbound-multiplier`, and accounting state/backups in `/var/lib/farstar-inbound-multiplier`.

```bash
systemctl status farstar-inbound-multiplier
journalctl -u farstar-inbound-multiplier -n 50 --no-pager
```

The original `restartXrayOnClientDisable` policy is not changed. If enabled, the official panel can still restart its core when an account is depleted.

After instrumentation, do not blindly remove the management redirect or stop the gateway: active scoped counters still need normalization. A full uninstall must restore factor 1, drain usage and clear runtime identities in a planned maintenance window, possibly with a controlled core restart. See [architecture](docs/architecture.md).

## Tests

```bash
mkdir -p generated
protoc -I proto --python_out=generated $(find proto -name '*.proto' -printf '%P ')
sudo python3 -m unittest -v test_accounting test_installer
sudo python3 test_installation.py
```

Run privileged integration tests on a test host. They use their own loopback listeners and systemd service. `XRAY_BIN` can point to a different core binary. The suite covers shared accounts, exact fractional accounting, access controls, live sockets, core restart recovery, fresh one-command installation and factor-preserving upgrades.

MIT for original companion code; upstream protocol definitions retain the included Xray license. Protocol sources are pinned in `proto/SOURCES.json`. No deployment credentials, real customer database or private keys are distributed.

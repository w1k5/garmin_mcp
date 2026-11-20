# Oracle Cloud deployment with ChatGPT OAuth

This directory adds a single-user Oracle Cloud deployment to the original [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) project. It includes a ChatGPT OAuth layer, a Caddy HTTPS proxy, systemd services, and menstrual log write tools. The fork retains the original history and license.

This is an independent, unofficial fork. It is not affiliated with Garmin or maintained by the upstream project.

## What changed in this fork

- Upgraded `garminconnect` to 0.3.19 for menstrual daily-log writes.
- Added tools for menstrual symptoms, moods, and the `LOW` / `AVERAGE` / `HIGH` Sex Drive field.
- Added `hosted.py` and `oauth_server.py` here to protect the MCP endpoint with a single-owner OAuth flow.
- Added a repeatable Ubuntu/OCI setup script and systemd/Caddy configuration.

The existing Garmin server remains the upstream project. This deployment is an optional addition.

## New Oracle VM

1. Create an Ubuntu VM with a persistent public IP. Allow inbound TCP 80 and 443 in the OCI network security rules. Restrict SSH (22) to your own IP. Point a DNS hostname at the VM; an `IP-with-dashes.sslip.io` hostname also works.
2. Install `git`, `uv`, Caddy, and OpenSSL from their official packages. `uv` supplies Python 3.12 for the pinned project. Verify `uv --version` and `caddy version` as the `ubuntu` user.
3. Clone this fork to `/opt/garmin-mcp/app`, owned by `ubuntu`. For example:

   ```sh
   sudo mkdir -p /opt/garmin-mcp
   sudo chown ubuntu:ubuntu /opt/garmin-mcp
   git clone https://github.com/w1k5/garmin_mcp.git /opt/garmin-mcp/app
   ```

4. Run setup with the public hostname (without `https://`):

   ```sh
   cd /opt/garmin-mcp/app
   bash deploy/oracle/setup.sh YOUR_HOSTNAME
   ```

   Setup installs the locked Python environment, creates protected runtime directories and a random 64-character server key, installs systemd services, and starts HTTPS. If `/etc/garmin-mcp.env` already exists, it keeps the existing key. Review the script before running it because it writes system configuration and starts services with `sudo`.

5. Sign in to Garmin on the VM as `ubuntu`:

   ```sh
   cd /opt/garmin-mcp/app
   GARMINTOKENS=/srv/garmin-mcp/data/garmin-tokens .venv/bin/garmin-mcp-auth
   ```

   Enter the Garmin password and MFA code only at the interactive prompt. Do not put them in shell history, Git, or the environment file.

6. Check service health with `bash deploy/oracle/check.sh`. The HTTPS `/healthz` response confirms the host is reachable; a Garmin read tool confirms the Garmin token is valid.

## Connect ChatGPT

The MCP endpoint is `https://YOUR_HOSTNAME/mcp`. The server supports OAuth dynamic client registration with the `garmin.personal` scope. During ChatGPT approval, enter the **server access key** from `/etc/garmin-mcp.env` into the **Server access key** field. This is separate from Garmin credentials. Never paste the key into a chat or commit it.

On the VM, retrieve the key with:

```sh
sudo sed -n 's/^MCP_API_KEY=//p' /etc/garmin-mcp.env
```

Copy the output directly into ChatGPT's **Server access key** field. Anyone with this key can authorize access to your Garmin data, so keep it private and avoid saving it in shell history or screenshots.

The server offers read and write tools, including `set_menstrual_sex_drive(date, sex_drive)` with `LOW`, `AVERAGE`, or `HIGH`. This tool preserves other fields in the day's menstrual log and reads the saved value back. Client apps may cache their tool list; reconnect or start a new chat after deploying new tools.

## Update the existing VM

```sh
cd /opt/garmin-mcp/app
git pull --ff-only
bash deploy/oracle/setup.sh YOUR_HOSTNAME
```

The setup script preserves `/etc/garmin-mcp.env` and `/srv/garmin-mcp/data`. Back up both before replacing the VM. Keep the data directory private: it includes Garmin tokens and the OAuth database. Restoring the OAuth database requires the same server access key, because the key also protects stored OAuth records. If the key changes, reconnect ChatGPT.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| HTTPS unavailable | OCI ingress rules for 80/443, DNS, `systemctl status caddy`, Caddy logs |
| MCP returns 401 | ChatGPT connection expired; reconnect with the current server access key |
| Approval says “failed” | Confirm the exact current server key; start a fresh approval request and avoid repeated failed attempts |
| Garmin tool errors | Run `garmin-mcp-auth` again, then inspect `journalctl -u garmin-mcp` |
| New tool absent in ChatGPT | Verify it appears in the live MCP tool list, then refresh the app connection or start a new chat |

## Licenses

The project remains under its original MIT license. `garminconnect` is installed from its public package; its source is not vendored here.

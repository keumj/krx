# LAN deployment

This mode runs the FastAPI service on this PC and allows access from devices on
the same router/private network.

## 1. Install dependencies

```cmd
setup_service.cmd
```

## 2. Start the LAN service

```cmd
run_lan_service.cmd
```

Or:

```powershell
.\run_lan_service.ps1
```

The script prints URLs like:

```text
Local:   http://localhost:8515
LAN:     http://192.168.0.15:8515
```

Other devices on the same router should use the `LAN` URL.

## 3. Allow Windows Firewall on private networks

Run Command Prompt as Administrator, then:

```cmd
allow_lan_firewall.cmd
```

The rule opens only the Private network profile for TCP port `8515`.

## 4. Configuration

Copy `.env.example` to `.env` when you need local settings:

```cmd
copy .env.example .env
```

Default LAN settings:

```env
KEUMJM_HOST=0.0.0.0
KEUMJM_PORT=8515
KEUMJM_ACCESS_MODE=lan
KEUMJM_ENABLE_DOCS=true
```

In `lan` mode, the app rejects public client IPs at the application layer. It
allows localhost, private network IPs, link-local IPs, and any extra CIDR ranges
listed in `KEUMJM_ALLOWED_CIDRS`.

## 5. Later internet mode

For public internet exposure, do not expose Uvicorn directly. Put the app behind
HTTPS and a reverse proxy such as Caddy, Nginx, IIS, or Cloudflare Tunnel.

Recommended changes for internet mode:

```env
KEUMJM_ACCESS_MODE=internet
KEUMJM_HOST=127.0.0.1
KEUMJM_ENABLE_DOCS=false
```

Then let the reverse proxy handle the public HTTPS port and forward traffic to
`127.0.0.1:8515`.

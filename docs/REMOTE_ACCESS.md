# Remote access (off-LAN) — Cloudflare Tunnel + Access

Use SignalBerry away from home WiFi, behind CGNAT, **without a VPS and without
opening any inbound ports**. The BlackBerry Q10 can't run a VPN (the BB10
Android runtime has no VpnService), so the path is: an outbound Cloudflare
Tunnel from the home server to Cloudflare's edge, with Cloudflare Access +
a bridge token gating who can reach it.

## Why both auth layers

Two backends get exposed through the tunnel:

| Backend         | What it can do            | Gate                                   |
|-----------------|---------------------------|----------------------------------------|
| signal-cli-rest-api (`:8080`) | send as you, read everything | **Cloudflare Access only** (no built-in auth) |
| bridge (`:9099`)              | the change feed          | **Cloudflare Access + bearer token**   |

Cloudflare Access is therefore **required**, not optional — it's the only thing
standing in front of the Signal REST API. The bridge token is a second lock on
the bridge in case the Access policy is ever misconfigured.

TLS to a 2013 device: the app bundles **Conscrypt**, which gives it TLS 1.3 +
modern certs regardless of the ancient OS stack (verified on a real Q10).

---

## One-time setup

### 1. Cloudflare account + domain
- A Cloudflare account, and a domain whose nameservers point to Cloudflare
  (any domain you control — the Free plan is enough).

### 2. Create the tunnel (dashboard)
- Zero Trust dashboard → **Networks → Tunnels → Create a tunnel** → *Cloudflared*.
- Name it (e.g. `signalberry`). Copy the **tunnel token** it shows.
- Add it to `/opt/docker/stacks/signalberrybridge/.env`:
  ```
  CF_TUNNEL_TOKEN=<the long token>
  BRIDGE_TOKEN=<the bridge token from chat>
  ```
- Under the tunnel's **Public Hostnames**, add two:
  | Subdomain   | Domain        | Service                  |
  |-------------|---------------|--------------------------|
  | `sb-api`    | `yourdomain`  | `http://signal-api:8080` |
  | `sb-bridge` | `yourdomain`  | `http://bridge:9099`     |
  (Service points at the **container name + internal port**, reachable because
  cloudflared shares the compose network.)

### 3. Start the tunnel
```
cd /opt/docker/stacks/signalberrybridge
docker compose --profile remote up -d        # starts cloudflared
docker compose up -d bridge                   # picks up BRIDGE_TOKEN
```

### 4. Cloudflare Access — service token + policy
- Zero Trust → **Access → Service Auth → Service Tokens → Create**.
  Name it (e.g. `signalberry-q10`). Copy the **Client ID** and **Client Secret**
  (the secret is shown once).
- Zero Trust → **Access → Applications → Add an application → Self-hosted**.
  - Application domain: `sb-api.yourdomain`. Add a **second** domain in the same
    app (or a second app) for `sb-bridge.yourdomain`.
  - Policy: **Action = Service Auth**, include → **Service Token** → the one above.
  - This makes both hostnames reject anything without the service-token headers,
    *at Cloudflare's edge, before it reaches your server.*

### 5. Enter it in the app
On the SignalBerry connect screen, expand **Remote access (Cloudflare)**:
- **Server (top field):** `https://sb-api.yourdomain`
- **Bridge URL:** `https://sb-bridge.yourdomain`
- **Bridge token:** the `BRIDGE_TOKEN` value
- **CF Access Client ID / Secret:** from step 4

Tap **Connect**. The app sends the Access headers to both hostnames and the
bearer token to the bridge, all over Conscrypt TLS 1.3.

---

## Notes
- **LAN still works**: leave the remote fields blank and use `192.168.1.x:5000`
  as before — no creds means no headers, unchanged behaviour.
- **Rotate** a leaked secret in the Cloudflare dashboard (service token) or by
  changing `BRIDGE_TOKEN` in `.env` + `docker compose up -d bridge` and updating
  the app.
- The credentials live only in app prefs and the server `.env` — never in git.
- The tunnel is **outbound only**; nothing is port-forwarded, CGNAT is irrelevant.

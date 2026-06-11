# Remote access (off-LAN)

Use SignalBerry away from home WiFi. The design is **transport-agnostic**: you
expose two local services however you like — Cloudflare Tunnel, Tailscale,
WireGuard, a VPS reverse proxy, plain port-forward — and a single shared
**bearer token** protects both, so you are not tied to any one provider.

## The two services and how they're protected

| Service                         | Built-in auth | How it's protected for remote use |
|---------------------------------|---------------|-----------------------------------|
| bridge (`:9099`)                | yes (token)   | `SB_AUTH_TOKEN` bearer, enforced in-process |
| signal-cli-rest-api (`:5000`)   | **none**      | `signal-api-auth` proxy (`:5001`) enforcing the same token |

The app sends `Authorization: Bearer <SB_AUTH_TOKEN>` on every request, plus the
modern TLS (Conscrypt) needed by old BlackBerry hardware. So for remote use you
expose **port 5001** (the auth proxy) and **port 9099** (the bridge) — never
signal-api's raw 5000.

## Activate the token + proxy

1. Put a strong secret in `.env`:
   ```
   SB_AUTH_TOKEN=<long random string>
   ```
2. Bring up the auth proxy and re-create the bridge so it picks up the token:
   ```
   docker compose --profile remote up -d signal-api-auth
   docker compose up -d bridge
   ```
3. Now expose **`localhost:5001`** and **`localhost:9099`** with whatever
   transport you choose (examples below).
4. In the app's **Remote access** section:
   - **Server:** `https://<your-signal-api-host>`   (fronts port 5001)
   - **Bridge URL:** `https://<your-bridge-host>`    (fronts port 9099)
   - **Bridge token:** the `SB_AUTH_TOKEN` value
   - CF Access fields: only if you also use Cloudflare Access (optional, below)

LAN is unaffected: leave the remote fields blank, point at `192.168.1.x:5000`,
and nothing changes (no token → no headers → original behaviour).

---

## Transport options (pick one)

### Cloudflare Tunnel (no public IP / CGNAT-friendly, no open ports)
- Create a tunnel (Zero Trust → Networks → Tunnels). If running cloudflared in
  this compose, set `CF_TUNNEL_TOKEN` in `.env` and `docker compose --profile
  remote up -d cloudflared`; if running it natively on the host, just run that
  tunnel there.
- Add two **public hostnames**, pointing at the host-published ports:
  | Hostname            | Service URL              |
  |---------------------|--------------------------|
  | `sb-api.<domain>`   | `http://localhost:5001`  |
  | `sb-bridge.<domain>`| `http://localhost:9099`  |
- Cloudflare provides TLS at the edge; the app talks to it over Conscrypt TLS 1.3.
- *Optional extra layer:* Cloudflare Access with a service token gives edge-level
  filtering on top of the bearer token. If you use it, paste the Access Client
  ID/Secret into the app's CF fields. Not required — the bearer token already
  protects both services.

### Tailscale / WireGuard / other VPN
- Put the home server on the VPN; reach `100.x.y.z:5001` and `:9099` from the
  phone *if* the phone can run the VPN. (Note: the BlackBerry Q10's BB10 Android
  runtime can't run a VPN client — Cloudflare Tunnel is the practical pick there.)

### VPS reverse proxy
- A small VPS with a real IP, reverse-proxying (and/or TLS-terminating) to the
  home server over your own WireGuard/SSH tunnel. Point it at `:5001` and `:9099`.

In every case the bearer token is the constant protection; the transport just
moves bytes.

---

## Notes
- One secret, two services: `SB_AUTH_TOKEN` (legacy alias `BRIDGE_TOKEN` still
  read). Rotate it in `.env` + `docker compose up -d bridge signal-api-auth` and
  update the app.
- Secrets live only in `.env` and app prefs — never in git (`.env` is gitignored).
- The auth-proxy token check is a simple string compare (not constant-time);
  fine for a personal deployment behind a tunnel.

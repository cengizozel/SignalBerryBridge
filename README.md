# SignalBerry Bridge

The persistence and catch-up layer for [SignalBerry](https://github.com/cengizozel/SignalBerry),
a native Signal client for BlackBerry 10.

The bridge sits between [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api)
and the app. It listens to the Signal receive WebSocket, writes every message and
event into SQLite, and serves a monotonic change feed so the app can replay exactly
what it missed while it was offline. It is a small Python service with no
dependencies beyond Flask, a WebSocket client, and waitress.

## Why it exists

signal-cli does not store messages. It delivers each one once over a WebSocket and
forgets it. A phone client that was closed, asleep, or out of signal would simply
miss whatever happened while it was away, including reactions to old messages and
read receipts that arrive with no message body of their own.

The bridge fixes that by being the thing that never forgets. It records every
message, reaction, edit, delete, quote, and receipt status, and stamps each change
with an ever increasing sequence number (`mod_seq`). The app keeps the last number
it saw and asks for everything after it, so reconnecting is a small delta, not a
full resync, and nothing is replayed twice. This is why the app opens a chat
instantly instead of waiting on a sync.

## How it works

A few design choices keep it correct and small.

- **Single writer.** Every WebSocket envelope and every app report funnels through
  one write path under one lock, so the sequence number and the row it labels
  always commit together. Readers never see a half applied change.
- **A frozen v1 surface and a v2 surface.** The original endpoints and the `messages`
  table are kept byte identical for backward compatibility. All current behaviour
  lives in `messages_v2` and the `/v2` endpoints, with the `mod_seq` change feed.
- **App reports its own sends.** signal-cli does not echo a linked device's own
  sends back to it, so the app posts each send to the bridge (`/v2/sent`), which
  folds it into history. This is why your sent messages and their delivery and read
  receipts line up across restarts.
- **Identifier sanity.** Phone numbers, UUIDs, service ids (the `PNI:` form sent
  when a contact changes phones), and group ids each normalize to a stable key, so
  the same person never splits into two conversations. This logic lives in
  `sigshapes.py` and is unit tested on its own.

## Setup (home network)

**1. Configure.** Create a `.env` file, which is gitignored:

```
SIGNAL_NUMBER=+12223334444
```

**2. Start the stack:**

```bash
docker compose up -d --build
```

This starts two containers: `signal-api` (signal-cli-rest-api) on port 5000, and
`signal-bridge` (this service) on port 9099.

**3. Link your Signal account.** Open this on the Docker host and scan the QR from
your phone under Signal, Settings, Linked Devices, the plus button:

```
http://YOUR_HOST:5000/v1/qrcodelink?device_name=signal-api
```

Confirm:

```bash
curl http://YOUR_HOST:5000/v1/accounts   # should return your number
```

**4. Connect the app.** In SignalBerry enter your Signal number, the signal-cli URL
`YOUR_HOST:5000`, and the bridge URL `YOUR_HOST:9099`. On a trusted home network you
can leave the bridge token off.

## Using it away from home

To reach the bridge and signal-cli over the internet, you expose them through a
tunnel or VPN or reverse proxy and protect them with a shared token. The full
walkthrough is in [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md). The short version:

1. Put a strong secret in `.env` as `SB_AUTH_TOKEN`.
2. Bring up the bundled auth proxy and re-create the bridge:
   ```bash
   docker compose --profile remote up -d signal-api-auth
   docker compose up -d bridge
   ```
3. Expose the proxy port (default 5001, set `API_AUTH_PORT` if taken) and the
   bridge port 9099 with your transport of choice, and enter the public addresses
   plus the token in the app.

## Security

- **Bridge token.** With `SB_AUTH_TOKEN` set, every request must carry it as a
  bearer token or get a 401, checked in constant time. Unset means open, which is
  fine on a trusted LAN and not for the internet.
- **Auth proxy for signal-cli.** signal-cli-rest-api has no auth of its own, so the
  `signal-api-auth` nginx service enforces the same token in front of it, including
  on the WebSocket upgrade. Expose the proxy, never signal-cli's raw 5000.
- **No secrets in git.** `.env` holds the token and is gitignored. The repo ships
  `.env.example` with placeholders only.

See [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md) for the complete model, including
the optional Cloudflare Access layer and how the app's bundled Conscrypt gives a
2013 device modern TLS.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SIGNAL_NUMBER` | *(required)* | Your Signal number in E.164 format |
| `SB_AUTH_TOKEN` | *(empty)* | Shared bearer token. Empty disables auth (LAN only). Protects both the bridge and the auth proxy. |
| `API_AUTH_PORT` | `5001` | Host port for the signal-cli auth proxy |
| `CF_TUNNEL_TOKEN` | *(empty)* | Only if you use the bundled Cloudflare tunnel service |
| `SIGNAL_API_URL` | `http://signal-api:8080` | Internal URL of signal-cli-rest-api |
| `DB_PATH` | `/data/bridge.db` | SQLite database path |
| `PORT` | `9099` | Port the bridge listens on |

## HTTP API

The app uses the `/v2` surface. The change feed is the heart of it.

### `GET /v2/changes?since_seq=<n>&limit=<n>`

Returns every message and marker with `mod_seq` greater than `since_seq`, in order,
plus the current `max_seq`. The app advances `since_seq` to the last row it
received and loops until empty. This is the catch-up mechanism.

### `POST /v2/sent`

The app reports a message it just sent so the bridge can fold it into history
(signal-cli does not echo own sends). Carries an edit form and a deleted form too.

### `POST /v2/read-receipts`

Server side read receipt fan out for messages the app has read.

### `POST /v2/purge`

Wipes message data, optionally for a single peer, and VACUUMs so the bytes are
gone rather than lingering in free pages. Login, contacts, and the phone's own
history are untouched.

### `GET /health`

Returns status including `api_version`, which the app checks on connect to warn
about a bridge and app version mismatch.

The v1 endpoints (`GET /messages`, `GET /unread`, `POST /read`) are frozen for
backward compatibility and are not used by the current app.

## Development

```bash
python -m pytest tests/      # 54 tests: envelope handling, change feed, auth, sigshapes
```

`bridge.py` is the service. `sigshapes.py` holds the pure, environment free
identifier and envelope shape helpers, kept separate so they can be unit tested
without a database or Flask. `remote/` holds the auth proxy config template.

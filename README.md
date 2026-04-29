# SignalBerry Bridge

A lightweight persistence layer between [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) and the [SignalBerry](https://github.com/your-username/SignalBerry) Android app.

Connects to the Signal REST API via WebSocket, stores every message in SQLite, and exposes them over a simple HTTP API for the app to poll.

## How it works

```
Signal ──► signal-cli-rest-api ──► Bridge (WebSocket listener)
                                        │
                                    SQLite DB
                                        │
                          Android app ◄─┘ (HTTP polling on :9099)
```

## Setup

### 1. Configure environment

Create a `.env` file (never commit this):

```
SIGNAL_NUMBER=+1234567890
```

### 2. Start the stack

```bash
docker compose up -d --build
```

This starts two containers:
- `signal-api` — Signal REST API on port `5000`
- `signal-bridge` — this bridge on port `9099`

### 3. Link your Signal account

Open in a browser:

```
http://YOUR_HOST:5000/v1/qrcodelink?device_name=signal-api
```

Then on your phone: **Signal → Settings → Linked Devices → "+" → scan the QR code**.

Verify it worked:

```bash
curl http://YOUR_HOST:5000/v1/accounts
# should return your number
```

### 4. Connect the Android app

In the SignalBerry app enter `YOUR_HOST:5000` as the API host and your Signal number, then tap Connect.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SIGNAL_NUMBER` | *(required)* | Your Signal number in E.164 format |
| `SIGNAL_API_URL` | `http://signal-api:8080` | Internal URL of signal-cli-rest-api |
| `DB_PATH` | `/data/bridge.db` | SQLite database path |
| `PORT` | `9099` | HTTP port the bridge listens on |

## HTTP API

### `GET /messages`

| Param | Description |
|---|---|
| `peer` | Phone number or UUID of the contact |
| `after` | Only return messages with `serverTs` greater than this (ms) |
| `limit` | Max messages to return (default 50, max 1000) |

**Response:**
```json
{
  "items": [
    { "dir": "in", "text": "hey", "serverTs": 1700000000000, "status": 2 },
    { "dir": "out", "text": "hello", "serverTs": 1700000001000, "status": 1 }
  ]
}
```

### `GET /health`

Returns `{"ok": true}` when the bridge is running.

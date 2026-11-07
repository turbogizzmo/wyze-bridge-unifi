# wyze-bridge-unifi

A customized Wyze camera bridge with ONVIF support for UniFi Protect integration.

This repo is a **standalone fork** combining:
- [mrlt8/docker-wyze-bridge](https://github.com/mrlt8/docker-wyze-bridge) — core Python bridge (v3.12.x era)
- ONVIF server additions (originally from IDisposable's wyze-bridge-onvif work)

Both upstream repos have since moved away from this Python architecture. This fork preserves and fixes the Python+ONVIF combination specifically for adopting Wyze cameras into **UniFi Protect via ONVIF Advanced Adoption**.

## What this does

Runs a Docker container on your network that:
1. Connects to your Wyze cameras using the Wyze P2P/TUTK protocol
2. Re-publishes streams via RTSP (MediaMTX)
3. Exposes an ONVIF server (port 8000) so UniFi Protect can discover and adopt cameras natively

## ⚠️ Newer Wyze firmware broke TUTK? (`IOTC_ER_TIMEOUT`)

Some Wyze firmware updates (e.g. Bulb Cam `HL_BC` **21.1.6.1161**, and various v3/v4
builds) disable the local **TUTK** protocol this bridge relies on. The bridge then loops
with `TUTK Error [-13] IOTC_ER_TIMEOUT` and never produces a stream, even though the
camera works fine in the Wyze app.

There's an additional, drop-in workaround that streams the camera over Wyze's
WebRTC/Kinesis path (via **go2rtc**) into this bridge's existing MediaMTX/ONVIF pipeline —
**no changes to the bridge itself**. See **[`go2rtc-kvs/`](go2rtc-kvs/)** for the deploy
guide and ready-to-run compose stack.

## Fixes applied in this fork

### Python 3.12 pin (`docker/Dockerfile`)
Upstream defaulted to Python 3.13, which removed the `cgi` module. The `spyne` SOAP library (used by the ONVIF server) still depends on `cgi`, so this fork pins to `python:3.12-slim-bookworm` and installs `ca-certificates`.

### SSL certificate fix (`app/wyzecam/api.py`)
The bundled `certifi` certificate store failed to verify DigiCert intermediates used by `api.wyzecam.com`. Patched to use a `requests.Session` pointed at the system CA bundle (`/etc/ssl/certs/ca-certificates.crt`), with the host cert file mounted into the container.

### MediaMTX auth fix (`app/wyzebridge/mtx_server.py`)
MediaMTX 1.12.3 requires a `user` field on every `authInternalUsers` entry — empty usernames cause a fatal startup error. Fixed `setup_auth` to:
- Set `authInternalUsers: []` (allow all, no auth) when `WB_AUTH=false` and no `STREAM_AUTH` is set
- Always include `"user": "any"` on the publisher localhost entry
- Include `{"action": "read"}` permissions on `parse_auth` entries (missing permission caused reads to be rejected)

### ONVIF `_get_actual_stream_settings` operator precedence bug (`app/wyzebridge/onvif_server.py`)
```python
# Before (Python parsed this as a 3-tuple assignment):
width, height = 640, 480 if bitrate < 500 else 1280, 720

# After:
width, height = (640, 480) if bitrate < 500 else (1280, 720)
```

### ONVIF authentication (`app/wyzebridge/onvif_server.py`)
The ONVIF server verifies credentials on every request except `GetSystemDateAndTime` (which ONVIF clients call before logging in to sync clocks). It accepts:
- WS-Security UsernameToken with `PasswordDigest` (what UniFi Protect and most ONVIF clients send) or `PasswordText`
- HTTP Digest, and HTTP Basic

Requests with missing or wrong credentials get an ONVIF `NotAuthorized` fault (HTTP 400 for a bad UsernameToken, HTTP 401 with a Digest challenge otherwise). The `/onvif/test` and `/onvif/discovery_test` status pages need the same login.

Which credentials are accepted:
- By default, any `STREAM_AUTH` pair, plus `wb` / `<WB_API>` when `WB_AUTH=true`. These are the same logins MediaMTX accepts for RTSP, so UniFi Protect can use one username/password for both ONVIF and RTSP.
- If `ONVIF_USERNAME` and `ONVIF_PASSWORD` are both set (env vars or Docker secrets), only that pair is accepted for ONVIF.

If none of these is configured, the ONVIF server rejects every authenticated call and logs an error.

`GetStreamUri` embeds RTSP credentials in the returned URL only for an authenticated caller: the pair it logged in with, or, when it used `ONVIF_USERNAME`/`ONVIF_PASSWORD`, the first RTSP login. For example:
```
rtsp://wyze:<password>@192.168.1.x:8554/driveway
```

## Deployment (docker-compose)

```yaml
services:
  wyze-bridge:
    build:
      context: .
      dockerfile: docker/Dockerfile
      args:
        BUILD: release
    image: wyze-bridge-unifi:local
    container_name: wyze-bridge
    restart: unless-stopped
    network_mode: host
    environment:
      - WYZE_EMAIL=your@email.com
      - WYZE_PASSWORD=yourpassword
      - API_ID=your-api-id
      - API_KEY=your-api-key
      - WB_IP=<bridge-host-ip>
      - WB_AUTH=true                 # Web UI/API login; see "Web UI / API authentication"
      # - WB_PASSWORD=<strong-password>
      # - WB_API=<long-random-key>   # required by go2rtc-kvs (e.g. openssl rand -hex 24)
      - STREAM_AUTH=wyze:CHANGE_ME   # credentials UniFi Protect will use for ONVIF + RTSP; use a strong password
      - QUALITY=sd30                 # use sd30 for Wyze Bulb Cam (HL_BC), hd for others
      - ON_DEMAND=false
      - FILTER_NAMES=driveway        # comma-separated camera names to enable
      - ONVIF_ENABLE=true
      - ONVIF_PORT=8000
      - ONVIF_DISCOVERY=true
      - LOG_LEVEL=info
    volumes:
      - ./data:/data
      - ./tokens:/tokens             # keeps Wyze tokens and generated WB_PASSWORD/WB_API
      - /etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro
```

## Web UI / API authentication

`WB_AUTH=true` (the default, and what the sample compose sets) puts the Web UI and REST API on port 5000 behind a login:
- Web UI: HTTP Basic auth as `WB_USERNAME` (default `wbadmin`) / `WB_PASSWORD`.
- API: send the key as an `api` header or `?api=` query parameter. It works for `/api`, `/img`, `/snapshot`, `/thumb`, `/photo` and `/signaling`.
- With `WB_AUTH=true` MediaMTX also accepts `wb` / `<WB_API>` for RTSP/HLS/WebRTC, in addition to any `STREAM_AUTH` pairs.
- All of those logins can only *watch*. Only clients on `127.0.0.1` inside the bridge's network can *publish* into a camera path without a password. To let a relay elsewhere publish (for example the [go2rtc-kvs](go2rtc-kvs/README.md) relay on Docker Desktop or on another host), set `STREAM_PUBLISH_AUTH=user:pass`. It uses the same format as `STREAM_AUTH` (`user:pass[:ip,ip][@cam,cam]`, several entries separated by `|`), but those logins can only publish, not watch, and they are not accepted for ONVIF.

If `WB_PASSWORD` or `WB_API` is not set, the bridge generates a random value on first start and saves it to `/tokens/wb_password` / `/tokens/wb_api` (mode 0600). Read them with `docker exec wyze-bridge cat /tokens/wb_password /tokens/wb_api`. Mount `/tokens` (as above) or set both explicitly (env vars or Docker secrets) so they survive container re-creation.

`WB_AUTH=false` removes all authentication from port 5000: anyone on the network can control cameras, take snapshots, open a cloud WebRTC session and restart the bridge. Only use it on a network you fully trust.

## Adopting into UniFi Protect

1. Enable **Advanced Adoption** in UniFi Protect → Settings → Labs
2. Go to Devices → Add Device → Add ONVIF Camera
3. Enter `<bridge-ip>:8000` as the IP (the ONVIF port, not the RTSP port)
4. Enter the credentials from `STREAM_AUTH` (e.g. `wyze` / `<your STREAM_AUTH password>`), or `ONVIF_USERNAME` / `ONVIF_PASSWORD` if you set them
5. The camera will adopt as the Wyze model (e.g. `Wyze Labs HL_BC`)

## Camera notes

**Wyze Bulb Cam (HL_BC):** Only supports SD resolution (~30kbps). Use `QUALITY=sd30`. Using HD quality causes stream timeouts which UniFi Protect incorrectly reports as "Invalid credentials".

## Building

```bash
docker compose build
docker compose up -d
docker logs wyze-bridge -f
```

## Upgrading: security changes that affect existing setups

- **The sample compose now sets `WB_AUTH=true`** (it used to ship `WB_AUTH=false`). After updating, the Web UI asks for a login and API calls need the `WB_API` key. Set `WB_PASSWORD`/`WB_API` (see "Web UI / API authentication"). If you use `go2rtc-kvs`, add `&api=<WB_API>` to each stream URL in `go2rtc.yaml`. To keep the old open behavior, set `WB_AUTH=false` explicitly, knowing it leaves camera control open to the network.
- **The default Web UI password and API key are now random.** They used to be derived from the Wyze account email (password = the part before `@`, API key = a hash of the email), which anyone who knows the email could compute. If you relied on those defaults, set `WB_PASSWORD`/`WB_API` explicitly, or read the generated values from `/tokens`.
- **Weak `STREAM_AUTH` passwords are flagged.** The sample used to be `wyze:wyze`. The bridge still accepts such a password but logs a warning at startup; change it, then update the password in UniFi Protect.
- **go2rtc-kvs listens on loopback only.** Its API/UI (`:1984`) and RTSP (`:8561`) are no longer reachable from the LAN, and the image is pinned to a release. See [`go2rtc-kvs/README.md`](go2rtc-kvs/README.md#security-notes).
- **WS-Discovery is stricter.** The bridge now answers only well-formed ONVIF Probe messages from private, link-local or loopback addresses (RFC 1918, `fe80::/10`, `fc00::/7`), at most 10 per sender every 10 seconds. UniFi Protect on the same LAN is unaffected; a controller reaching the bridge from a public or CGNAT (`100.64.0.0/10`) address can still add the camera manually by IP.
- **Webhooks verify TLS certificates.** `*_WEBHOOKS` URLs over HTTPS used to skip certificate checks. Set `WEBHOOK_VERIFY_SSL=false` if your endpoint uses a self-signed certificate.
- **ONVIF now requires credentials.** Earlier builds accepted any (or no) login and handed out the RTSP password in `GetStreamUri` to anyone. If a camera was adopted with the `STREAM_AUTH` username/password, nothing changes. If Protect was given some other username/password, re-enter the `STREAM_AUTH` pair in Protect, or set `ONVIF_USERNAME`/`ONVIF_PASSWORD` to the login Protect already uses.

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

### ONVIF `GetStreamUri` credential embedding (`app/wyzebridge/onvif_server.py`)
The ONVIF spec requires the stream URI to include credentials when authentication is needed. The `GetStreamUri` response now reads `STREAM_AUTH` and embeds the first credential pair in the RTSP URL:
```
rtsp://wyze:wyze@192.168.1.x:8554/driveway
```
This is what allows UniFi Protect to authenticate after adopting a camera via Advanced Adoption.

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
      - WB_AUTH=false
      - STREAM_AUTH=wyze:wyze        # credentials UniFi Protect will use for RTSP
      - QUALITY=sd30                 # use sd30 for Wyze Bulb Cam (HL_BC), hd for others
      - ON_DEMAND=false
      - FILTER_NAMES=driveway        # comma-separated camera names to enable
      - ONVIF_ENABLE=true
      - ONVIF_PORT=8000
      - ONVIF_DISCOVERY=true
      - LOG_LEVEL=info
    volumes:
      - ./data:/data
      - /etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro
```

## Adopting into UniFi Protect

1. Enable **Advanced Adoption** in UniFi Protect → Settings → Labs
2. Go to Devices → Add Device → Add ONVIF Camera
3. Enter `<bridge-ip>:8000` as the IP (the ONVIF port, not the RTSP port)
4. Enter the credentials from `STREAM_AUTH` (e.g. `wyze` / `wyze`)
5. The camera will adopt as the Wyze model (e.g. `Wyze Labs HL_BC`)

## Camera notes

**Wyze Bulb Cam (HL_BC):** Only supports SD resolution (~30kbps). Use `QUALITY=sd30`. Using HD quality causes stream timeouts which UniFi Protect incorrectly reports as "Invalid credentials".

## Building

```bash
docker compose build
docker compose up -d
docker logs wyze-bridge -f
```

# go2rtc / KVS workaround — when Wyze firmware breaks TUTK

Some Wyze firmware updates (e.g. **Bulb Cam `HL_BC` 21.1.6.1161**, and various v3/v4
builds) **remove or break the local TUTK P2P protocol** that this bridge uses to pull a
camera's stream over the LAN. The camera keeps working in the Wyze app, but the bridge
loops forever and never produces video.

**Symptoms**
- Bridge log repeats: `TUTK Error [-13] IOTC_ER_TIMEOUT` and `⏰ Timed out connecting to <cam>`
- The stream cycles `connecting → stopped`; `rtsp://<bridge>:8554/<cam>` has no media
- In UniFi Protect the camera shows offline / no stream (sometimes "invalid credentials")
- The camera is **fine in the Wyze app**

## Why this happens & how the workaround fixes it

The bridge talks to the camera over **TUTK** (a direct, LAN-only P2P protocol). Firmware
updates have moved cameras off it. But the camera still streams over Wyze's
**WebRTC / AWS Kinesis Video (KVS)** path — the one the app uses when you're away from home.

[**go2rtc**](https://github.com/AlexxIT/go2rtc) can connect to that KVS path with its
`#format=wyze` adapter (which handles Wyze's non-standard SDP), and this bridge already
exposes the signaling the adapter needs at `GET /signaling/<cam>?kvs`.

```
camera ──Wyze cloud / AWS KVS──▶ go2rtc (#format=wyze) ──ffmpeg copy──▶ bridge MediaMTX ──▶ ONVIF ──▶ UniFi Protect
   (WebRTC, no TUTK)                RTSP :8561              RTSP :8554/<cam>     (unchanged)
```

The relay republishes go2rtc's stream into the bridge's **existing** MediaMTX path, so
**the ONVIF server and your Protect adoption need no changes** — the same
`rtsp://<bridge>:8554/<cam>` URL simply starts carrying video again.

### Trade-offs (read these)
- **Fixed 640×360 @ ~20 fps.** The KVS stream resolution can't be changed.
- **Cloud-dependent.** Media now rides Wyze's cloud + AWS (like the app). If your
  internet or Wyze's cloud is down, the stream is down. (Old TUTK was LAN-only.)
- Bandwidth is tiny; fine for SD cameras like the Bulb Cam.

## Prerequisites
- The `wyze-bridge` container is already running on this host with the web/API on `:5000`,
  MediaMTX RTSP on `:8554`, and (for Protect) the ONVIF server on `:8000`.
- `WB_AUTH=false` (or you can reach `/signaling/<cam>?kvs` without web auth).
- Docker + Docker Compose on the same host as the bridge.

## Deploy

1. **Find each camera's URI.** It's the last segment of the bridge's RTSP path and the
   `name_uri` field in the API:
   ```bash
   curl -s http://<BRIDGE_IP>:5000/api | grep -o '"name_uri":"[^"]*"'
   ```
   e.g. `driveway`, `front_yard_cam`.

2. **Add a stream per camera** in [`go2rtc.yaml`](go2rtc.yaml):
   ```yaml
   streams:
     driveway:       webrtc:http://127.0.0.1:5000/signaling/driveway?kvs#format=wyze
     front_yard_cam: webrtc:http://127.0.0.1:5000/signaling/front_yard_cam?kvs#format=wyze
   ```
   (Use `127.0.0.1` if go2rtc runs on the same host as the bridge; otherwise the bridge's IP.)

3. **List those same URIs** in [`docker-compose.yml`](docker-compose.yml):
   ```yaml
   environment:
     CAMERAS: "driveway,front_yard_cam"
   ```

4. **Start it:**
   ```bash
   docker compose up -d
   ```

5. **Verify** the bridge's RTSP path now has media (this is what Protect reads):
   ```bash
   # replace creds with your STREAM_AUTH; 'wyze:wyze' is the sample default
   docker exec wyze-go2rtc ffprobe -rtsp_transport tcp \
     "rtsp://wyze:wyze@127.0.0.1:8554/driveway"
   # expect: Stream ... Video: h264 ... 640x360 ... 20 fps
   ```
   In Protect the camera should start showing video within a retry cycle. If it was
   never adopted, adopt it now via ONVIF as usual (see the repo's main README).

## Home Assistant

HA ships **go2rtc natively** (since 2024.11; also available via the go2rtc add-on or
Frigate). You don't need the relay — just add the stream to HA's go2rtc config and point
a camera entity at it:

```yaml
streams:
  driveway: webrtc:http://<BRIDGE_IP>:5000/signaling/driveway?kvs#format=wyze
```

Then use it as a go2rtc/WebRTC camera source. The bridge add-on itself needs no changes.

## Troubleshooting
- **`ffprobe` on `:8561` (go2rtc) works but `:8554` (bridge) is empty** — the relay isn't
  publishing. Check `docker logs wyze-kvs-relay`; confirm `CAMERAS` matches the go2rtc
  stream names exactly.
- **Publish rejected / 401 on `:8554`** — your MediaMTX auth doesn't allow the localhost
  publisher. Ensure the bridge's default localhost-publish is intact, or set
  `MTX: "rtsp://<user>:<pass>@127.0.0.1:8554"` in the compose (use a `STREAM_AUTH` pair
  that has publish permission).
- **go2rtc gets no media** — open `http://<host>:1984`, check the stream. Confirm
  `curl http://<BRIDGE_IP>:5000/signaling/<cam>?kvs` returns `"result":"ok"`. If it
  returns 404 / "does not support WebRTC", that camera model has no KVS path.
- **Only some cameras work** — each must return `"result":"ok"` from the signaling
  endpoint; models without a KVS path can't use this workaround.

## Credits
Workaround approach first reported on the Wyze forums:
<https://forums.wyze.com/t/wyze-v4-docker-wyze-bridge-iotc-er-timeout-401-client-error/342218>

#!/bin/sh
# Relay each go2rtc KVS stream into the bridge's MediaMTX, so the EXISTING
# rtsp://<bridge>:8554/<camera_uri> path (which the ONVIF server already advertises
# to UniFi Protect) carries the video. No re-encode (-c copy). ONVIF stays untouched.
#
# The bridge's MediaMTX paths are `source: publisher` + `overridePublisher: true`
# and allow a localhost publisher, so the dead-TUTK path simply accepts our stream.
#
# Config via environment (see docker-compose.yml):
#   CAMERAS    comma-separated camera URIs, e.g. "driveway,front_yard_cam"
#   GO2RTC     go2rtc RTSP base            (default rtsp://127.0.0.1:8561)
#   MTX        bridge MediaMTX RTSP base   (default rtsp://127.0.0.1:8554)

set -eu
: "${CAMERAS:?set CAMERAS=cam1,cam2 (comma-separated camera URIs)}"
GO2RTC="${GO2RTC:-rtsp://127.0.0.1:8561}"
MTX="${MTX:-rtsp://127.0.0.1:8554}"

relay_one() {
  cam="$1"
  while true; do
    echo "[relay] $cam: $GO2RTC/$cam  ->  $MTX/$cam"
    ffmpeg -hide_banner -loglevel warning \
      -rtsp_transport tcp -i "$GO2RTC/$cam" \
      -c copy -f rtsp -rtsp_transport tcp "$MTX/$cam" || true
    echo "[relay] $cam: ffmpeg exited; restarting in 5s"
    sleep 5
  done
}

# Launch one resilient ffmpeg per camera.
OLDIFS=$IFS; IFS=','
for c in $CAMERAS; do
  [ -n "$c" ] && relay_one "$c" &
done
IFS=$OLDIFS
wait

# dth-to-minio

Captures a DTH set-top box feed via USB HDMI capture card on a Raspberry Pi 4B, records it into 1-minute H.264 MKV clips, and uploads them to a MinIO (S3-compatible) server over the local network.

## Architecture

```
┌─────────────┐    USB     ┌──────────────┐    LAN     ┌──────────────┐
│  DTH Set-Top │───HDMI───▶│ Capture Card  │──────────▶│  MinIO Server │
│  Box (STB)   │           │ (RPi 4B)      │  upload   │  (NAS/other)  │
└─────────────┘           └──────────────┘           └──────────────┘
                                  │
                           ffmpeg capture
                           + segment mux
                           + watchdog upload
```

### Data flow

```
ffmpeg reads V4L2 (MJPEG) + ALSA audio
        │
        ▼
libx264 encode → 60s .mkv segments → ./segments/
        │
        ▼
watchdog detects file close (inotify IN_CLOSE_WRITE)
        │
        ▼
ffprobe validates: has video + audio + duration ≥ 55s
        │
        ▼
upload to MinIO → channel/YYYYMMDD/filename.mkv
        │
        ▼
delete local file
```

## Hardware setup

| Component | Details |
|---|---|
| Board | Raspberry Pi 4B (4GB recommended) |
| OS | Raspberry Pi OS (Debian Bookworm, 64-bit) |
| Capture device | USB HDMI capture card presenting as `/dev/video0` (V4L2, MJPEG) |
| Audio | USB audio from capture card (ALSA, e.g. `hw:3,0` — find yours with `arecord -l`) |
| Python | 3.11+ via [uv](https://docs.astral.sh/uv/) |

### Finding your device names

```bash
# Video devices
v4l2-ctl --list-devices

# Audio devices
arecord -l

# Test video capture
ffplay -f v4l2 -input_format mjpeg -video_size 1280x720 -framerate 30 -i /dev/video0

# Test audio capture
ffplay -f alsa -i hw:3,0
```

## Encoder history (important context)

We tested three approaches on this specific RPi 4B + capture card setup:

| Approach | Result | Notes |
|---|---|---|
| `-c:v copy` (MJPEG passthrough) | **Working but 250+ MB/min** | No re-encode, huge files. MJPEG stores every frame as a full JPEG. |
| `-c:v h264_v4l2m2m` (hardware H.264) | **Rainbow/corrupted output** | Hardware encoder doesn't support MJPEG input directly. Even with `-vf format=yuv420p` it produced artifacts. Segment muxer also had SPS/PPS header issues with this encoder. |
| `-c:v libx264` (software H.264) | **Working, ~15-25 MB/min** | CPU usage acceptable on RPi 4B at 720p30 with `veryfast` preset. This is the current production config. |

**Do not switch to hardware H.264 without extensive testing.** The rainbow issue is device-specific and may not appear on all capture cards.

## Quick start

```bash
# Clone
git clone <repo> && cd dth-to-minio

# Install deps
uv sync

# Run (foreground, for testing)
MINIO_ENDPOINT=192.168.1.10:9000 \
MINIO_ACCESS_KEY=minioadmin \
MINIO_SECRET_KEY=minioadmin \
MINIO_BUCKET=cnbc-awaaz-segments \
uv run capture.py
```

## Configuration

All config is via environment variables (or edit the `CONFIG` block in `capture.py`).

### Capture

| Env var | Default | Description |
|---|---|---|
| `VIDEO_DEV` | `/dev/video0` | V4L2 video device |
| `AUDIO_CARD` | `hw:3,0` | ALSA audio device |
| `CHANNEL_NAME` | `cnbc-awaaz` | Name prefix for filenames and MinIO object keys |
| `RESOLUTION` | `1280x720` | Capture resolution |
| `FRAMERATE` | `30` | Capture framerate |

### Encoder

| Env var | Default | Description |
|---|---|---|
| `X264_PRESET` | `veryfast` | x264 preset (`ultrafast`, `superfast`, `veryfast`, `faster`, `fast`, `medium`) |
| `VIDEO_CRF` | `28` | Constant Rate Factor (0=lossless, 51=worst, 23=default, 28=good quality/size tradeoff) |
| `AUDIO_BITRATE` | `96k` | AAC audio bitrate |
| `SEGMENT_SECS` | `60` | Segment duration in seconds |

### MinIO

| Env var | Default | Description |
|---|---|---|
| `MINIO_ENDPOINT` | `192.168.1.10:9000` | MinIO server address |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO secret key |
| `MINIO_BUCKET` | `iptv-segments` | Target bucket name |
| `MINIO_SECURE` | `false` | Use HTTPS (`true`/`false`) |

### Upload & validation

| Env var | Default | Description |
|---|---|---|
| `UPLOAD_RETRIES` | `5` | Max upload attempts per file |
| `UPLOAD_RETRY_DELAY` | `5` | Base delay between retries (multiplied by attempt number) |
| `STALE_WARN_SECS` | `90` | Warn if no new segment in this many seconds |
| `MIN_SEGMENT_SECS` | `55` | Minimum valid segment duration — shorter files are rejected |

### File size estimates

| Encoder | CRF/bitrate | ~Size per minute |
|---|---|---|
| MJPEG copy | N/A | ~250 MB |
| libx264 veryfast | CRF 28 | ~15-25 MB |
| libx264 veryfast | CRF 23 | ~30-50 MB |
| libx264 veryfast | CRF 32 | ~8-15 MB |

Actual size depends on video content (static news slides = smaller, fast motion = larger).

## Deployment (systemd)

```bash
# On RPi 4B, copy files
scp -r . rpi4b:~/dth-to-minio/

# SSH to RPi
ssh rpi4b

# Install deps
cd ~/dth-to-minio && uv sync

# Edit systemd service for your environment
sudo cp iptv-capture.service /etc/systemd/system/
sudo systemctl daemon-reload

# Enable + start
sudo systemctl enable iptv-capture
sudo systemctl start iptv-capture

# Check logs
journalctl -u iptv-capture -f
```

## MinIO object layout

```
cnbc-awaaz-segments/          (bucket)
└── cnbc-awaaz/               (channel)
    └── 20260612/             (date)
        ├── cnbc-awaaz_20260612_090000.mkv
        ├── cnbc-awaaz_20260612_090100.mkv
        └── ...
```

## Validation & error handling

### Segment validation (ffprobe)

Every segment is validated via `ffprobe` before upload. A segment is rejected (renamed to `.mkv.bad`) if:

- File is too small (< 1 MB)
- No video stream detected
- No audio stream detected
- Duration < `MIN_SEGMENT_SECS` (default 55s)
- ffprobe can't parse the file

Bad files are kept locally as `.mkv.bad` for inspection — they are not uploaded and not retried.

### ffmpeg auto-restart

If ffmpeg crashes, it auto-restarts with progressive backoff (2s, 4s, 6s... up to 30s). This handles temporary device disconnections. Check `journalctl` if capture stops entirely.

### Stale segment watchdog

A background thread monitors `./segments/` and warns if no new segment appears within `STALE_WARN_SECS`. This catches ffmpeg stalls (process alive but not writing).

## Troubleshooting

| Problem | Check |
|---|---|
| No video | `v4l2-ctl --list-devices` — device may have moved to `/dev/video1` |
| No audio | `arecord -l` — update `AUDIO_CARD` |
| Rainbow/garbled output | Don't use `h264_v4l2m2m` with this capture card. Use `libx264`. |
| Huge files | You're using MJPEG copy. Switch to `libx264`. |
| Segments too short | Capture card may drop frames. Check `dmesg` for USB errors. |
| Upload fails | Check MinIO is reachable: `curl http://192.168.1.10:9000/minio/health/live` |
| CPU too high | Raise `VIDEO_CRF` (e.g. 32) or use `X264_PRESET=ultrafast` |
| Audio-only uploads | Fixed — we now validate segments via ffprobe before upload |

## Manual ffmpeg tests

Use these to debug before modifying `capture.py`:

```bash
# Test 10s capture with exact production settings
ffmpeg \
  -f v4l2 -input_format mjpeg -video_size 1280x720 -framerate 30 -i /dev/video0 \
  -f alsa -i hw:3,0 \
  -t 10 \
  -vf format=yuv420p \
  -c:v libx264 -preset veryfast -crf 28 -g 60 -keyint_min 60 -sc_threshold 0 \
  -c:a aac -b:a 96k -ar 48000 -ac 2 \
  test-manual.mkv

# Inspect output
ffprobe test-manual.mkv
ls -lh test-manual.mkv
```

## Dependencies

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/) (system package)
- [minio](https://pypi.org/project/minio/) Python client
- [watchdog](https://pypi.org/project/watchdog/) for filesystem events
- [uv](https://docs.astral.sh/uv/) for package management

## License

Internal project — not published.

"""
cnbc-iptv: DTH capture → 1-minute .mp4 clips → MinIO
=====================================================
Two independent threads:
  1. ffmpeg   — captures V4L2/ALSA input, writes 60s .mkv segments to ./segments/
  2. Uploader — watches ./segments/ for completed .mkv files, remuxes to .mp4,
                uploads .mp4 to MinIO, deletes local copies

Usage:
    uv run capture.py

Config via environment variables (or edit the CONFIG block below).
"""

import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from minio import Minio
from minio.error import S3Error
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("iptv")

# ── Config ─────────────────────────────────────────────────────────────────────
# Override any of these with environment variables, e.g.:
#   MINIO_ENDPOINT=192.168.1.10:9000 uv run capture.py

BASE_DIR     = Path(__file__).parent
SEGMENTS_DIR = BASE_DIR / "segments"

CONFIG = {
    # Capture device (from `v4l2-ctl --list-devices` and `arecord -l`)
    "video_dev":    os.getenv("VIDEO_DEV",    "/dev/video0"),
    "audio_card":   os.getenv("AUDIO_CARD",   "hw:3,0"),
    "channel_name": os.getenv("CHANNEL_NAME", "cnbc-awaaz"),

    # ffmpeg encode settings
    "resolution":   os.getenv("RESOLUTION",   "1280x720"),
    "framerate":    os.getenv("FRAMERATE",     "30"),
    "video_bitrate":os.getenv("VIDEO_BITRATE", "1500k"),
    "segment_secs": int(os.getenv("SEGMENT_SECS", "60")),

    # MinIO
    "minio_endpoint":   os.getenv("MINIO_ENDPOINT",   "192.168.1.10:9000"),
    "minio_access_key": os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
    "minio_secret_key": os.getenv("MINIO_SECRET_KEY", "minioadmin"),
    "minio_bucket":     os.getenv("MINIO_BUCKET",     "iptv-segments"),
    "minio_secure":     os.getenv("MINIO_SECURE",     "false").lower() == "true",

    # Upload retry
    "upload_retries":      int(os.getenv("UPLOAD_RETRIES",      "5")),
    "upload_retry_delay":  int(os.getenv("UPLOAD_RETRY_DELAY",  "5")),  # seconds between retries

    # Stale segment watchdog — warn if no new segment after this many seconds
    "stale_warn_secs": int(os.getenv("STALE_WARN_SECS", "90")),
}

# ── Setup ──────────────────────────────────────────────────────────────────────
SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)


# ── ffmpeg segment writer ──────────────────────────────────────────────────────

def build_ffmpeg_cmd() -> list[str]:
    segment_pattern = str(
        SEGMENTS_DIR / f"{CONFIG['channel_name']}_%Y%m%d_%H%M%S.mkv"
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        # Video input
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-video_size", CONFIG["resolution"],
        "-framerate", CONFIG["framerate"],
        "-i", CONFIG["video_dev"],
        # Audio input
        "-f", "alsa",
        "-i", CONFIG["audio_card"],
        # Video — keep camera MJPEG as-is. Hardware H.264 path produced rainbow output
        # on this capture device, while MJPEG passthrough is known-good.
        "-c:v", "copy",
        # Audio encode
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
        # Segment muxer
        "-f", "segment",
        "-segment_time", str(CONFIG["segment_secs"]),
        "-segment_format", "matroska",
        "-reset_timestamps", "1",
        "-strftime", "1",
        segment_pattern,
    ]


def run_ffmpeg():
    """
    Runs ffmpeg in a loop. Auto-restarts on crash with a 2s delay.
    This is the capture process — it should never stop.
    """
    cmd = build_ffmpeg_cmd()
    log.info(f"ffmpeg command: {' '.join(cmd)}")

    consecutive_failures = 0

    while True:
        log.info(f"[ffmpeg] Starting capture → {SEGMENTS_DIR}")
        start_time = time.monotonic()

        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        )

        # Stream stderr line-by-line so we see ffmpeg warnings in real time
        assert proc.stderr is not None  # guaranteed by stderr=subprocess.PIPE
        for line in proc.stderr:
            decoded = line.decode(errors="replace").rstrip()
            if decoded:
                log.warning(f"[ffmpeg] {decoded}")

        proc.wait()
        uptime = time.monotonic() - start_time

        if uptime < 5:
            consecutive_failures += 1
        else:
            consecutive_failures = 0  # reset on any meaningful run

        log.error(
            f"[ffmpeg] Process exited (rc={proc.returncode}, "
            f"uptime={uptime:.1f}s, consecutive_failures={consecutive_failures})"
        )

        # Back off if crashing immediately (bad device, wrong params, etc.)
        delay = min(2 * consecutive_failures, 30)
        if delay > 0:
            log.info(f"[ffmpeg] Waiting {delay}s before restart...")
            time.sleep(delay)


def stale_segment_watchdog():
    """
    Background thread — warns if no new segment appears within stale_warn_secs.
    Indicates ffmpeg has stalled without crashing.
    """
    while True:
        time.sleep(CONFIG["stale_warn_secs"])
        mp4_files = sorted(SEGMENTS_DIR.glob("*.mp4"))
        mkv_files = sorted(SEGMENTS_DIR.glob("*.mkv"))
        all_files = mp4_files + mkv_files
        if not all_files:
            continue
        latest = sorted(all_files, key=lambda f: f.stat().st_mtime)[-1]
        age = time.time() - latest.stat().st_mtime
        if age > CONFIG["stale_warn_secs"]:
            log.warning(
                f"[watchdog] No new segment in {age:.0f}s — "
                f"ffmpeg may be stalled. Latest: {latest.name}"
            )


# ── MKV → MP4 remux ───────────────────────────────────────────────────────────

def remux_to_mp4(src_path: Path) -> Path | None:
    """
    Remux a .mkv segment to .mp4 using stream copy (no re-encode).
    Returns the path to the new .mp4 file, or None on failure.
    """
    mp4_path = src_path.with_suffix(".mp4")
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(src_path),
        "-c", "copy",
        "-movflags", "+faststart",
        "-y",
        str(mp4_path),
    ]
    try:
        log.info(f"[remux] {src_path.name} → {mp4_path.name}")
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            log.error(f"[remux] ✗ Failed (rc={result.returncode}): {stderr}")
            return None
        # Verify output exists and is non-trivial
        if not mp4_path.exists() or mp4_path.stat().st_size < 1024:
            log.error(f"[remux] ✗ Output missing or too small: {mp4_path.name}")
            return None
        # Remove source .mkv after successful remux
        src_path.unlink()
        log.info(f"[remux] ✓ Done, deleted source: {src_path.name}")
        return mp4_path
    except subprocess.TimeoutExpired:
        log.error(f"[remux] ✗ Timed out after 30s: {src_path.name}")
        return None
    except Exception as e:
        log.error(f"[remux] ✗ Unexpected error: {e}")
        return None


# ── MinIO uploader ─────────────────────────────────────────────────────────────

def get_minio_client() -> Minio:
    return Minio(
        CONFIG["minio_endpoint"],
        access_key=CONFIG["minio_access_key"],
        secret_key=CONFIG["minio_secret_key"],
        secure=CONFIG["minio_secure"],
    )


def ensure_bucket(client: Minio):
    bucket = CONFIG["minio_bucket"]
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        log.info(f"[minio] Created bucket: {bucket}")
    else:
        log.info(f"[minio] Bucket exists: {bucket}")


def upload_with_retry(filepath: Path) -> bool:
    """
    Uploads a single .mp4 clip to MinIO with retries.
    Returns True on success, False after all retries exhausted.
    Deletes the local file only on confirmed upload success.
    """
    bucket   = CONFIG["minio_bucket"]
    filename = filepath.name
    # Object key: channel/YYYYMMDD/filename.mp4  — easy to browse in MinIO
    date_str   = datetime.now(timezone.utc).strftime("%Y%m%d")
    object_key = f"{CONFIG['channel_name']}/{date_str}/{filename}"

    for attempt in range(1, CONFIG["upload_retries"] + 1):
        try:
            client = get_minio_client()
            file_size = filepath.stat().st_size
            log.info(
                f"[upload] Attempt {attempt}/{CONFIG['upload_retries']}: "
                f"{filename} ({file_size / 1024 / 1024:.1f} MB) → {bucket}/{object_key}"
            )
            client.fput_object(
                bucket_name=bucket,
                object_name=object_key,
                file_path=str(filepath),
                content_type="video/mp4",
            )
            log.info(f"[upload] ✓ Success: {object_key}")
            filepath.unlink()
            log.info(f"[upload] ✓ Deleted local: {filename}")
            return True

        except S3Error as e:
            log.error(f"[upload] ✗ S3Error on attempt {attempt}: {e}")
        except FileNotFoundError:
            log.warning(f"[upload] File disappeared before upload: {filename}")
            return True  # nothing to do
        except Exception as e:
            log.error(f"[upload] ✗ Unexpected error on attempt {attempt}: {e}")

        if attempt < CONFIG["upload_retries"]:
            delay = CONFIG["upload_retry_delay"] * attempt  # progressive backoff
            log.info(f"[upload] Retrying in {delay}s...")
            time.sleep(delay)

    log.error(
        f"[upload] ✗ GAVE UP after {CONFIG['upload_retries']} attempts: {filename}. "
        f"File kept locally at {filepath}"
    )
    return False


# ── Watchdog file event handler ────────────────────────────────────────────────

class SegmentHandler(FileSystemEventHandler):
    """
    Handles new .mkv files written by ffmpeg's segment muxer.
    ffmpeg closes the file when a segment is complete — we queue it for
    remux → upload in the main loop.
    """

    def __init__(self, upload_queue: list):
        self._queue = upload_queue
        self._lock  = threading.Lock()

    def _handle(self, path: str | bytes):
        if isinstance(path, bytes):
            path = path.decode()
        if not path.endswith(".mkv"):
            return
        filepath = Path(path)
        if not filepath.exists():
            return
        # Ignore tiny files — segment still being written or empty
        if filepath.stat().st_size < 1024:
            return
        with self._lock:
            if path not in self._queue:
                log.info(f"[watcher] Queued for upload: {filepath.name}")
                self._queue.append(path)

    # inotify IN_CLOSE_WRITE — file fully written and closed by ffmpeg
    def on_closed(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    # Fallback for systems where on_closed isn't fired (older kernels)
    def on_created(self, event):
        if not event.is_directory:
            # Small delay to let ffmpeg finish writing
            time.sleep(0.5)
            self._handle(event.src_path)


def run_uploader():
    """
    Watches segments/ directory for completed .mkv files, remuxes to .mp4,
    and uploads to MinIO. Runs in the main thread after starting ffmpeg
    in a background thread.
    """
    client = get_minio_client()
    ensure_bucket(client)

    # Upload any leftover files from a previous run (e.g. after reboot)
    # Handle .mkv files first (remux then upload), then any .mp4 files
    leftover_mkv = sorted(SEGMENTS_DIR.glob("*.mkv"))
    leftover_mp4 = sorted(SEGMENTS_DIR.glob("*.mp4"))
    if leftover_mkv:
        log.info(f"[upload] Found {len(leftover_mkv)} leftover .mkv segment(s), remuxing...")
        for f in leftover_mkv:
            mp4 = remux_to_mp4(f)
            if mp4:
                upload_with_retry(mp4)
    if leftover_mp4:
        log.info(f"[upload] Found {len(leftover_mp4)} leftover .mp4 clip(s) from previous run")
        for f in leftover_mp4:
            upload_with_retry(f)

    upload_queue: list[str] = []
    handler  = SegmentHandler(upload_queue)
    observer = Observer()
    observer.schedule(handler, str(SEGMENTS_DIR), recursive=False)
    observer.start()
    log.info(f"[watcher] Watching {SEGMENTS_DIR}")

    try:
        while True:
            if upload_queue:
                mkv_path = Path(upload_queue.pop(0))
                # Step 1: Remux .mkv → .mp4
                mp4_path = remux_to_mp4(mkv_path)
                if mp4_path is None:
                    log.error(f"[pipeline] Remux failed, skipping: {mkv_path.name}")
                    continue
                # Step 2: Upload .mp4
                upload_with_retry(mp4_path)
            else:
                time.sleep(0.2)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        observer.stop()
        observer.join()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("IPTV Capture & Upload")
    log.info(f"  Channel  : {CONFIG['channel_name']}")
    log.info(f"  Video    : {CONFIG['video_dev']} @ {CONFIG['resolution']}p{CONFIG['framerate']}")
    log.info(f"  Audio    : {CONFIG['audio_card']}")
    log.info(f"  Segments : {SEGMENTS_DIR} ({CONFIG['segment_secs']}s each → .mp4 clips)")
    log.info(f"  MinIO    : {CONFIG['minio_endpoint']} / {CONFIG['minio_bucket']}")
    log.info("=" * 60)

    # ffmpeg capture thread (daemon — dies with main process)
    ffmpeg_thread = threading.Thread(target=run_ffmpeg, daemon=True, name="ffmpeg")
    ffmpeg_thread.start()

    # Stale segment watchdog thread
    watchdog_thread = threading.Thread(
        target=stale_segment_watchdog, daemon=True, name="watchdog"
    )
    watchdog_thread.start()

    # Give ffmpeg a moment to start writing before watcher starts
    time.sleep(3)

    # Uploader runs in main thread (blocks until KeyboardInterrupt)
    run_uploader()


if __name__ == "__main__":
    main()

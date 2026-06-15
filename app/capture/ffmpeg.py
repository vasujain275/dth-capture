"""ffmpeg capture process for DTH segments.

Responsibilities:
- Build ffmpeg command from settings
- Run ffmpeg in a loop with auto-restart
- Stale segment watchdog

Usage:
    from app.capture.ffmpeg import run_ffmpeg, stale_segment_watchdog
    from app.config import settings

    # Run in daemon threads:
    Thread(target=run_ffmpeg, args=(settings,), daemon=True).start()
    Thread(target=stale_segment_watchdog, args=(settings,), daemon=True).start()
"""

import subprocess
import time
from pathlib import Path

from app.utils.logging import get_logger

logger = get_logger(__name__)

# ── Segments directory ────────────────────────────────────────────────────────
SEGMENTS_DIR = Path(__file__).resolve().parent.parent.parent / "segments"


def build_ffmpeg_cmd(settings) -> list[str]:
    """Build the ffmpeg command list from settings.

    Args:
        settings: Settings instance with all config fields

    Returns:
        List of command arguments for subprocess.Popen
    """
    SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)

    segment_pattern = str(
        SEGMENTS_DIR / f"{settings.CHANNEL_NAME}_%Y%m%d_%H%M%S.mkv"
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        # Video input
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-video_size", settings.RESOLUTION,
        "-framerate", str(settings.FRAMERATE),
        "-i", settings.VIDEO_DEV,
        # Audio input
        "-f", "alsa",
        "-i", settings.AUDIO_CARD,
        # Video encode — software H.264
        "-vf", "format=yuv420p",
        "-c:v", "libx264",
        "-preset", settings.X264_PRESET,
        "-crf", str(settings.VIDEO_CRF),
        "-g", str(settings.FRAMERATE * 2),  # keyframe every 2s
        "-keyint_min", str(settings.FRAMERATE * 2),
        "-sc_threshold", "0",
        # Audio encode
        "-c:a", "aac",
        "-b:a", settings.AUDIO_BITRATE,
        "-ar", "48000",
        "-ac", "2",
        # Segment muxer
        "-f", "segment",
        "-segment_time", str(settings.SEGMENT_SECS),
        "-segment_format", "matroska",
        "-reset_timestamps", "1",
        "-strftime", "1",
        segment_pattern,
    ]


def run_ffmpeg(settings) -> None:
    """Run ffmpeg in a loop with auto-restart.

    This is the capture process — it should never stop.
    On crash, it waits with progressive backoff before restarting.

    Args:
        settings: Settings instance
    """
    cmd = build_ffmpeg_cmd(settings)
    logger.info(f"ffmpeg command: {' '.join(cmd)}")

    consecutive_failures = 0

    while True:
        logger.info(f"[ffmpeg] Starting capture → {SEGMENTS_DIR}")
        start_time = time.monotonic()

        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        )

        # Stream stderr line-by-line for real-time warnings
        assert proc.stderr is not None
        for line in proc.stderr:
            decoded = line.decode(errors="replace").rstrip()
            if decoded:
                logger.warning(f"[ffmpeg] {decoded}")

        proc.wait()
        uptime = time.monotonic() - start_time

        if uptime < 5:
            consecutive_failures += 1
        else:
            consecutive_failures = 0  # reset on any meaningful run

        logger.error(
            f"[ffmpeg] Process exited (rc={proc.returncode}, "
            f"uptime={uptime:.1f}s, consecutive_failures={consecutive_failures})"
        )

        # Back off if crashing immediately (bad device, wrong params, etc.)
        delay = min(2 * consecutive_failures, 30)
        if delay > 0:
            logger.info(f"[ffmpeg] Waiting {delay}s before restart...")
            time.sleep(delay)


def stale_segment_watchdog(settings) -> None:
    """Background thread — warns if no new segment appears within stale_warn_secs.

    Indicates ffmpeg has stalled without crashing.

    Args:
        settings: Settings instance
    """
    while True:
        time.sleep(settings.STALE_WARN_SECS)
        all_files = sorted(SEGMENTS_DIR.glob("*.mkv"))
        if not all_files:
            continue
        latest = sorted(all_files, key=lambda f: f.stat().st_mtime)[-1]
        age = time.time() - latest.stat().st_mtime
        if age > settings.STALE_WARN_SECS:
            logger.warning(
                f"[watchdog] No new segment in {age:.0f}s — "
                f"ffmpeg may be stalled. Latest: {latest.name}"
            )

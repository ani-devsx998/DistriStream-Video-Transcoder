import subprocess
from pathlib import Path


def complexity_level(score):
    if score < 0.30:
        return "LOW"
    if score < 0.60:
        return "MEDIUM"
    if score < 0.80:
        return "HIGH"
    return "VERY_HIGH"


def analyze_chunk(chunk_path):
    """Measure low-resolution motion and spatial detail from a chunk."""
    width = 160
    height = 90

    process = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(chunk_path),
            "-vf",
            f"scale={width}:{height}",
            "-r",
            "2",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    frame_size = width * height
    frames = [
        process.stdout[offset:offset + frame_size]
        for offset in range(0, len(process.stdout), frame_size)
        if len(process.stdout[offset:offset + frame_size]) == frame_size
    ]

    if not frames:
        return {
            "motion": 0.0,
            "spatial": 0.0,
            "complexity": 0.0,
            "level": "LOW",
            "formula": "0.6M + 0.4S (saliency unavailable)",
        }

    motion_total = 0.0
    spatial_total = 0.0

    for frame_index, frame in enumerate(frames):
        spatial_sample = 0.0
        for y in range(1, height):
            row = y * width
            previous_row = (y - 1) * width
            for x in range(1, width):
                value = abs(frame[row + x] - frame[row + x - 1])
                value += abs(frame[row + x] - frame[previous_row + x])
                spatial_sample += value

        spatial_total += spatial_sample / ((width - 1) * (height - 1) * 510)

        if frame_index:
            previous = frames[frame_index - 1]
            motion_sample = sum(
                abs(current - old)
                for current, old in zip(frame, previous)
            ) / (frame_size * 255)
            motion_total += motion_sample

    spatial = min(1.0, spatial_total / len(frames))
    motion = min(1.0, motion_total / max(1, len(frames) - 1))

    # Slightly expand the useful range of these raw normalized measurements.
    motion = min(1.0, motion * 2.5)
    spatial = min(1.0, spatial * 2.0)
    complexity = min(1.0, 0.6 * motion + 0.4 * spatial)

    return {
        "motion": round(motion, 4),
        "spatial": round(spatial, 4),
        "complexity": round(complexity, 4),
        "level": complexity_level(complexity),
        "formula": "0.6M + 0.4S (saliency unavailable)",
    }


BITRATE_RANGES = {
    "1080p": (2500, 5000),
    "720p": (1500, 3000),
    "480p": (900, 1500),
    "360p": (500, 800),
}


def plan_bitrate(profile, complexity):
    minimum, maximum = BITRATE_RANGES[profile]
    return round(minimum + complexity * (maximum - minimum))

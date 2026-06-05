"""Shared 16-bit signed-LE PCM helpers for provider adapters.

Pure-stdlib so adapters ship without numpy / scipy. Mirrors the algorithm
used by :func:`johnny.meet_worker.audio_bridge.resample_pcm16`; the
duplication across packages is intentional so ``app/providers/`` stays
free of meet-worker imports (the API container ships providers without
the meet-worker package).
"""

from __future__ import annotations

import array

from app.providers.base import PCM_SAMPLE_WIDTH_BYTES


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit signed LE mono PCM via linear interpolation.

    Not anti-aliased: adequate for sub-second voice chunks where high-
    frequency aliasing on aggressive downsamples is acceptable.
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(
            f"sample rates must be positive: src={src_rate} dst={dst_rate}"
        )
    if len(pcm) % PCM_SAMPLE_WIDTH_BYTES:
        raise ValueError("PCM byte length must be even for 16-bit samples")
    if not pcm or src_rate == dst_rate:
        return pcm

    samples = array.array("h")
    samples.frombytes(pcm)
    src_len = len(samples)
    dst_len = max(1, round(src_len * dst_rate / src_rate))
    out = array.array("h", [0] * dst_len)

    if src_len == 1 or dst_len == 1:
        out[0] = samples[0]
        return out.tobytes()

    scale = (src_len - 1) / (dst_len - 1)
    for i in range(dst_len):
        src_idx = i * scale
        idx0 = int(src_idx)
        idx1 = idx0 + 1 if idx0 + 1 < src_len else idx0
        frac = src_idx - idx0
        value = samples[idx0] * (1.0 - frac) + samples[idx1] * frac
        if value > 32767.0:
            value = 32767.0
        elif value < -32768.0:
            value = -32768.0
        out[i] = int(value)

    return out.tobytes()


__all__ = ["resample_pcm16"]

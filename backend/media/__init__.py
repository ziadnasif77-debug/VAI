"""Media engine: probing, proxy, audio and frame extraction (Phase 2, SPEC §99).

FFmpeg and FFprobe are invoked here and nowhere else. Command lines are built
as explicit argument lists by trusted application code (§85).

The package is deliberately free of the database and of the job system: it
takes paths and configuration, writes files, and returns what it produced. The
pipeline workers are what turn that into persisted state, which keeps every
piece of media logic testable against a five-second test clip.

Everything is written to survive an eight-hour source (§7): probing reads
container metadata only, extraction streams, and the proxy is built in
resumable segments.
"""

from backend.media.audio import AudioStream, extract_all_tracks, extract_analysis_audio
from backend.media.chunking import Chunk, ChunkPlan, plan_chunks
from backend.media.ffmpeg import CancelledError, FFmpegRunner, ProgressReport
from backend.media.frames import (
    ExtractedFrame,
    SamplingPlan,
    extract_at_times,
    extract_uniform,
    plan_base_sampling,
    plan_candidate_sampling,
)
from backend.media.probe import ProbeResult, TrackInfo, probe_media
from backend.media.proxy import ProxyResult, generate_proxy

__all__ = [
    "AudioStream",
    "CancelledError",
    "Chunk",
    "ChunkPlan",
    "ExtractedFrame",
    "FFmpegRunner",
    "ProbeResult",
    "ProgressReport",
    "ProxyResult",
    "SamplingPlan",
    "TrackInfo",
    "extract_all_tracks",
    "extract_analysis_audio",
    "extract_at_times",
    "extract_uniform",
    "generate_proxy",
    "plan_base_sampling",
    "plan_candidate_sampling",
    "plan_chunks",
    "probe_media",
]

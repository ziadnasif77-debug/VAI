"""Upload-ready metadata derived from stored evidence (SPEC §50, §80).

Everything a YouTube upload form asks for -- title, description, tags,
chapters, thumbnail -- generated deterministically from what the pipeline
already measured and stored. No model runs here: §80's rule is that
explanations come from data, and a title is an explanation of the video.
"""

from backend.metadata.generation import (
    detect_transcript_language,
    suggest,
    thumbnail_arguments,
    thumbnail_peak,
)

__all__ = [
    "detect_transcript_language",
    "suggest",
    "thumbnail_arguments",
    "thumbnail_peak",
]

"""What the graphics card currently has left, and who took the rest.

§54 says one heavy model in VRAM at a time, and this pipeline keeps that
promise between its own stages: every provider unloads, and the Ollama ones
send ``keep_alive: 0`` because dropping a reference is not enough. What none
of that can control is **another program on the same machine**.

Measured on 2026-08-15: a ``qwen2.5-coder:7b`` left resident by something else
held 4.7 GB of this card's 8 GB, with an expiry in the year 2318 — nothing was
ever going to release it. Chromium then could not start, timed out after 25
seconds, and nineteen render-dependent tests failed across two full runs that
each took twice as long as usual. The render had assumed an empty card for the
life of the project and never once looked.

So this module looks. Everything here returns ``None`` rather than raising or
guessing: a machine with no NVIDIA card, no ``nvidia-smi``, or a driver that
times out is a machine this cannot speak about, and "unknown" must not read as
"empty" — the whole defect above was a confident assumption about free memory.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from backend.core.logging import LogChannel, get_logger

logger = get_logger("core.gpu", LogChannel.RENDERING)

#: Long enough for a busy driver, short enough not to stall a render start.
_QUERY_TIMEOUT_SECONDS: Final[float] = 8.0

#: Ollama's local API. Reporting is unrestricted; *releasing* is only ever
#: done by name, for the models this application is configured to use. A model
#: another program loaded is that program's to release, and unloading it
#: because it happens to be in the way would be taking the machine over.
_OLLAMA_PS_URL: Final[str] = "http://localhost:11434/api/ps"
_OLLAMA_GENERATE_URL: Final[str] = "http://localhost:11434/api/generate"
_OLLAMA_TIMEOUT_SECONDS: Final[float] = 2.0
_OLLAMA_RELEASE_TIMEOUT_SECONDS: Final[float] = 10.0


@dataclass(frozen=True, slots=True)
class ResidentModel:
    """A model Ollama is holding, and how much of the card it holds."""

    name: str
    vram_mb: int

    def __str__(self) -> str:
        return f"{self.name} ({self.vram_mb} MB)"


def release_everything_we_loaded(config: Any) -> dict[str, Any]:
    """Give the card back after the work that needed it is done.

    Called at two moments, and both were asked for in the same breath: before
    the render, so Chromium and NVENC get the card to themselves, and once a
    project has nothing left to run, so the machine is free afterwards. The
    pipeline is a sequence of specialists — Whisper, then a vision model, then
    a reasoning model, then a renderer — and none of them needs the previous
    one's memory.

    Returns what was done, so the caller can log a before and after rather
    than assert that it worked.
    """
    models = config.models
    before = free_vram_mb()
    released = release_models(
        [
            getattr(getattr(models, "vision", None), "model", ""),
            getattr(getattr(models, "llm", None), "model", ""),
            getattr(getattr(models, "reasoning", None), "model", ""),
        ]
    )
    release_local_caches()
    after = free_vram_mb()
    return {
        "released": released,
        "free_vram_mb_before": before,
        "free_vram_mb_after": after,
        "freed_mb": (after - before) if (before is not None and after is not None) else None,
    }


def free_vram_mb(device_index: int = 0) -> int | None:
    """Free VRAM in megabytes, or ``None`` when this machine cannot say.

    ``None`` covers every honest unknown: no card, no driver tool, a query
    that timed out. Callers must treat it as "do not decide on this", never as
    a number.
    """
    output = _query(
        [
            "nvidia-smi",
            f"--id={device_index}",
            "--query-gpu=memory.free",
            "--format=csv,noheader,nounits",
        ]
    )
    if output is None:
        return None
    first = output.splitlines()[0] if output.splitlines() else ""
    try:
        return int(float(first.strip()))
    except (TypeError, ValueError):
        return None


def resident_models() -> list[ResidentModel]:
    """Models Ollama currently holds in VRAM, largest first.

    Best-effort and read-only. This exists so a "the card is full" message can
    name the thing to close: an operator told *which model* is holding four
    gigabytes can act in seconds, where "out of memory" sends them hunting.
    """
    try:
        with urllib.request.urlopen(_OLLAMA_PS_URL, timeout=_OLLAMA_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return []
    if not isinstance(payload, dict):
        return []

    found: list[ResidentModel] = []
    for item in payload.get("models", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "").strip()
        size = item.get("size_vram") or 0
        if not name or not isinstance(size, (int, float)) or size <= 0:
            continue
        found.append(ResidentModel(name=name, vram_mb=int(size) // (1024 * 1024)))
    found.sort(key=lambda model: model.vram_mb, reverse=True)
    return found


def release_models(names: Iterable[str]) -> list[str]:
    """Hand back the VRAM held by these models. Returns the ones released.

    Unconditional by design. Each provider already unloads in a ``finally``,
    but only when *that instance* was the one that loaded the model: a model
    left resident by a previous run, a process that was killed, or a stage
    that ran in another worker is invisible to it and stays on the card
    forever. This asks Ollama by name, so it works whoever loaded it.

    Cheap and idempotent — unloading a model that is not loaded is a no-op
    Ollama answers in milliseconds — so a caller never has to know the state
    to be allowed to ask.

    Only the names given. This never sweeps the card clean: see the note on
    :data:`_OLLAMA_GENERATE_URL`.
    """
    released: list[str] = []
    for name in dict.fromkeys(name.strip() for name in names if name and name.strip()):
        payload = json.dumps({"model": name, "prompt": "", "keep_alive": 0}).encode("utf-8")
        request = urllib.request.Request(
            _OLLAMA_GENERATE_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=_OLLAMA_RELEASE_TIMEOUT_SECONDS
            ) as response:
                response.read()
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            # Ollama not running is the common case on a machine that only
            # transcribes, and is not a problem worth a stack trace.
            logger.debug(
                "Could not ask Ollama to release a model",
                extra={"model": name, "error": str(error)[:120]},
            )
            continue
        released.append(name)
    return released


def release_local_caches() -> None:
    """Drop this process's own CUDA allocations.

    CTranslate2 frees device memory when the model object is finalised, which
    is why the speech provider forces a collection rather than dropping a
    reference. Torch keeps its own allocator cache on top of that, and a
    cached block is memory the card cannot give to Chromium.
    """
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():  # pragma: no cover - needs a GPU
            torch.cuda.empty_cache()
    except Exception:  # torch is optional; its absence is not a failure
        return


def our_models(config: Any) -> frozenset[str]:
    """The model tags this project is configured to load, and only those.

    The set exists so that everything else on the card can be named as
    somebody else's. Measured on this machine: the shared Ollama store holds
    five models, and only two of them are VAI's. `qwen2.5-coder:7b` belongs to
    an OpenHands install that reaches the same daemon from Docker through
    `host.docker.internal:11434` -- the same tag that was once found resident
    with an expiry in the year 2318, holding 4.7 GB while a render waited.
    """
    models = getattr(config, "models", None)
    named = {
        str(getattr(getattr(models, attribute, None), "model", "") or "")
        for attribute in ("vision", "llm", "reasoning")
    }
    return frozenset(name for name in named if name)


def foreign_models(config: Any) -> list[ResidentModel]:
    """Models on the card that this project did not put there.

    **Reported, never released.** Another program's model is not this
    project's to unload: it may be mid-request, and taking its memory would be
    the exact discourtesy this project asks for in return. What is owed is a
    plain sentence naming what is holding the card, early enough to matter.
    """
    ours = our_models(config)
    return [model for model in resident_models() if model.name not in ours]


def contention(config: Any) -> dict[str, Any]:
    """What the card is holding, and how much of it belongs to somebody else.

    Answers the question §54 has always assumed away: "one heavy model at a
    time" is honoured between this project's own stages and says nothing about
    the rest of the machine. A render that fails after twenty minutes because
    another program's model was resident is the worst way to learn it -- so
    this is cheap enough to call at a stage boundary, and its whole output is a
    sentence somebody can act on.
    """
    free_mb = free_vram_mb()
    others = foreign_models(config)
    held = sum(model.vram_mb for model in others)
    return {
        "free_vram_mb": free_mb,
        "foreign_models": [str(model) for model in others],
        "foreign_vram_mb": held,
        "message": (
            f"{held} MB of the card is held by {', '.join(str(m) for m in others)}, "
            "which this project did not load and will not unload"
            if others
            else describe_pressure(free_mb)
        ),
    }


def describe_pressure(free_mb: int | None) -> str:
    """A sentence an operator can act on, for a message about a full card."""
    parts = []
    if free_mb is not None:
        parts.append(f"{free_mb} MB of video memory is free")
    holders = resident_models()
    if holders:
        listed = ", ".join(str(model) for model in holders[:3])
        parts.append(f"Ollama is holding {listed}")
    return "; ".join(parts) if parts else "the card's memory could not be read"


def _query(argv: list[str]) -> str | None:
    """Run a driver query, or return ``None`` if it cannot be run."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


__all__ = [
    "ResidentModel",
    "describe_pressure",
    "free_vram_mb",
    "release_everything_we_loaded",
    "release_local_caches",
    "release_models",
    "resident_models",
]

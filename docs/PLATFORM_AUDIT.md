# Platform Audit — what VAI already is, and how the Animation Studio stands on it

*2026-08-29. Ordered scope: Audit → Architecture extraction → P0. Nothing else
is authorised; the rabbit, SDXL and the worker fleet wait for P0's verdict.*

VAI was built as a gaming editor, but eleven of its subsystems are not about
gaming at all — they are about running a media pipeline honestly on this
machine. This audit names what is kernel, what is gaming, where the seams
actually are, and the extraction strategy that risks the least.

## 1. Kernel inventory (reusable as-is or near)

| Subsystem | Where | Coupling to gaming | Verdict |
| --- | --- | --- | --- |
| Job system: queue/start/complete/retry/requeue, §47 resume, recovery | `services/job_manager.py`, `database/repositories/jobs.py` | **One hard seam**: `JobStage` enum + `stages_in_order()` hardcode the gaming graph | Kernel after seam #1 |
| Background worker loop (one job at a time, own DB thread) | `services/worker.py` | Builds `default_workers()` (gaming registry) | Kernel after seam #1 |
| Daily policy: ledger, Oslo clock, caps, publishAt, Ferdig, reports | `services/daily_producer.py` | Reads gaming media table for pre-policy seeding only | Kernel — parameterise the discovery suffixes |
| Publisher: YouTube resumable upload, scheduled publishAt, retries, metadata contract | `publishing/*`, `pipeline/workers/publish_worker.py` | Metadata *generation* is gaming; transport is not | Kernel (transport) + per-product metadata writer |
| Technical QA: black/frozen/loudness/silence/streams + doctrine score plumbing | `qa/technical.py`, `qa_worker._doctrine_summary` | Content checks are gaming; decode checks are universal | Kernel (technical) + per-product content/creative QC |
| Audio mastering: ducking, LUFS, music shelves, synthesized SFX | `rendering/audio_mix.py`, `rendering/sfx.py` | None that matters | Kernel |
| FFmpeg runner + NVENC encoder selection + probe | `media/ffmpeg.py`, `rendering/encoder.py` | None | Kernel |
| Config loader (yaml→pydantic, env overrides) | `config/loader.py`, `schema.py` | **Seam #2**: one monolithic `AppConfig` | Kernel after seam #2 |
| Observability: channel logging, §95 notes, §80 reasons | `core/logging.py` + conventions | None | Kernel |
| Secrets discipline (data-root `.credentials/`, packager strip) | conventions + `scripts/package.py` | None | Kernel |
| SQLite + migrations + WAL discipline | `database/*` | Schema is gaming-shaped | Kernel machinery; each product owns tables |

Gaming-only (stays put): `analysis/`, `gaming/`, `moments/`, `narrative/`,
`timeline/`, `effects/`, `critic/`, `director/`, `evidence/`, `metadata/`
(the writers), `quality/` (golden sets).

## 2. The five real seams

1. **`JobStage` is a closed enum and the graph lives beside it.**
   `stages_in_order()` + per-stage dependency logic assume the gaming
   pipeline. The animation product needs its own graph
   (`story→scenes→assets_check→voice→render→master→qc→review→publish`).
   *Extraction shape*: stage-graph-as-data (a registry the job manager is
   handed) — but **not in P0**; P0 needs no job system at all.
2. **`AppConfig` is monolithic.** Adding `animation:` sections is additive and
   cheap (the `daily:` section proved it); a true split waits.
3. **`WorkerContext` carries gaming paths.** Fine — context is cheap to build
   per product once the stage registry is data.
4. **QA worker mixes universal decode checks with gaming content checks.**
   The decode half (`qa/technical.py`) imports cleanly already — the daily
   source probe used it from another package with zero friction.
5. **The publish worker regenerates gaming metadata on `auto`.** The
   transport/scheduling half is product-agnostic; the `_suggested` half is
   per-product by design.

## 3. Extraction strategy — strangler, not surgery

**Do not physically move code now.** A `platform/` package extracted today
would be an abstraction with one consumer and a guess about the second.
Instead:

- **P0 (now): zero-dependency prototype.** `animation/` talks to Godot and
  FFmpeg by argv only. It imports *nothing* from `backend/` — the point of P0
  is the renderer's quality and determinism, and it must not wait on any
  refactor. One deliberate exception is allowed if convenient: reusing
  `FFmpegRunner` read-only.
- **P1: adapter, not fork.** The animation pipeline becomes a second stage
  registry handed to the *existing* job manager/worker (seam #1 turned into
  data). Config gains `animation:` sections (seam #2, additive). QA reuses
  `qa/technical.py` directly and adds its own creative checks.
- **Physical `platform/` split: only at two proven consumers**, when the
  seams have been exercised by real code, not predicted.

This is the same discipline that built VAI: behaviour first, structure when
the second caller exists.

## 4. What P0 proves (and its declared tolerance)

Per the approved architecture v2: same Scene JSON + same assets + same
settings → same scene state → same frames → same audio → same visual output.
The harness renders the scene **twice** and compares frame-by-frame:

- Pass: ≥ 99.5% of frames byte-identical PNGs, and every differing frame
  PSNR ≥ 48 dB (GPU raster nondeterminism allowance).
- Audio: sample-identical (it is authored, not captured).
- MP4 byte-identity: explicitly **out of contract** (encoder/container
  metadata).

P0's second gate is human: if the placeholder character reads as a robotic
puppet, the animation engine gets fixed *here*, before any factory exists.

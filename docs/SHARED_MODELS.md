# Shared model infrastructure — how VAI stands today

**Investigation, not a change.** Nothing in this document has been implemented.
Measured 2026-08-15 against the running machine.

---

## 0. The headline, measured

```
F:\Models        36.4 GB   5 models   last written today, 13:54
D:\Models        36.4 GB   5 models   last written 2026-06-20
D:\VAI\models     4.4 GB   Whisper weights, project-local
D:\nav\.cache\huggingface           a fourth root, for HF downloads
```

**36.4 GB is duplicated exactly** — the same five Ollama models in two stores.
The machine's environment says `OLLAMA_MODELS=F:\Models`; `scripts/rooted.py`
line 94 says `D:/Models`. The June store is VAI's doing, and the code that
made it is still in the tree.

The duplication is not currently *active* — the running Ollama serves from
F: — but it is one cleared environment variable away from happening again,
and this Python process proves the gap is real: it sees `OLLAMA_MODELS =
D:\Models` right now, because it inherited a shell older than the machine
setting and `rooted.py` filled the blank with its own default.

---

## 1. Which models this project uses

| Capability | Model | Runtime | Where it loads from | VRAM |
| --- | --- | --- | --- | --- |
| Speech (§14) | `large-v3-turbo` | faster-whisper / CTranslate2, **in-process** | `D:\VAI\models` (project-local, 4.4 GB) | ~2 GB |
| Vision (§15) | `qwen2.5vl:7b` | Ollama, **HTTP** `localhost:11434` | `F:\Models` (via the service) | ~6 GB |
| Reasoning (§19, §93) | `qwen2.5:7b-instruct` | Ollama, **HTTP** | `F:\Models` | ~4.7 GB |
| OCR (§25) | `en_PP-OCRv4` | PaddleOCR/EasyOCR, **in-process** | library default cache | small |

Configured in `config/models.yaml`; every provider is built behind the §13
abstraction (`SpeechProvider`, `VisionProvider`, `OcrProvider`, plus the LLM
provider), so *what* runs is configuration, not code.

---

## 2. Where model startup happens

There is no application-level model startup. VAI **never starts the Ollama
server** — `grep` for `ollama serve`, `Popen`, `subprocess` against Ollama
returns nothing. It talks to whatever is already listening on 11434, and
degrades through §95 when nothing answers.

Loading is per stage, inside the worker that needs it:

```
speech_workers.py:170   provider.unload()   (finally)
vision_workers.py:339   provider.unload()   (finally)
gaming_workers.py:125   provider.unload()   (finally)
```

Each provider loads on first use and unloads in a `finally`. The two Ollama
providers send `keep_alive: 0`, which is what actually returns VRAM rather
than waiting out a timeout.

Since 2026-08-15 there is also an unconditional release before the render and
after a project finishes (`backend/core/gpu.py`), because a provider's own
unload only fires when *that instance* did the loading.

**Assessment against the instructions:** §3 ("no independent model
management") and §11 ("do not start models at application startup") are
already satisfied. VAI is a consumer of a shared runtime, not an owner of one.

---

## 3. How VAI detects running models today

| Question | Answered by | Where |
| --- | --- | --- |
| Which models are installed? | `GET /api/tags` | `health.py:373`, both Ollama providers |
| Which are loaded in VRAM? | `GET /api/ps` | `core/gpu.py:42` |
| How much VRAM is free? | `nvidia-smi --query-gpu=memory.free` | `core/gpu.py` |
| Which process owns a model? | **not answered** | — |
| Which project is using it? | **not answered** | — |
| Can this request reuse it? | **not asked** — the model name is taken from config and used | — |

So half the runtime picture exists and is already used to make decisions (the
render refuses to start when the card is full, and names the resident model).
What is missing is *ownership*: nothing on this machine records which project
asked for a model or is mid-request against it.

---

## 4. Can these models be shared?

| Runtime | Shareable | Why |
| --- | --- | --- |
| Ollama (vision, reasoning) | **Yes, already** | One server process, HTTP, many clients. Two projects calling `qwen2.5vl:7b` hit one loaded copy. The shared architecture the instructions ask for is what Ollama *is*. |
| faster-whisper (speech) | **No, as built** | Loaded in-process via CTranslate2. Two projects transcribing at once means two copies in VRAM, and neither can see the other. Sharing needs a server in front of it. |
| PaddleOCR | **No, as built** | Same shape; but it is small and CPU-friendly, so the cost of not sharing is low. |

The important consequence: **the expensive models are already shared and the
cheap ones are not.** Whisper large-v3-turbo is ~2 GB, against 6 GB for the
VLM which is already pooled.

---

## 5. What must change

Ordered by what the measurements justify, not by what the instructions list.

### 5.1 Stop pointing Ollama anywhere — **done, 2026-08-15**

`scripts/rooted.py` no longer writes `OLLAMA_MODELS` at all. It reads the
variable, reports it as `[shared]`, and reports it as *unset* when it is —
because an invented default is exactly what duplicated the store. Ten tests in
`tests/unit/test_rooted.py` now hold the line, including one that asserts no
model path is invented anywhere else in the environment.

`D:\Models` was deleted after confirming the two stores were byte-identical:
same five manifests, same twenty-two blobs, same 36.4 GB, with `F:` carrying
today's timestamp and the machine's own `OLLAMA_MODELS`. **Reclaimed 36.7 GB**
(D: free 1208.5 → 1245.2 GB); Ollama still serves all five models.

### 5.2 Move Whisper's weights out of the project (small)

`D:\VAI\models` holds 4.4 GB of Hugging Face-format Whisper weights, and
`HF_HOME` points at a fourth root. §1 forbids project-local copies. Both should
resolve under the shared repository — `F:\Models\huggingface` or similar — so a
second project transcribing does not download the same 4.4 GB again.

Note the constraint this must respect: F: has **67 GB free** against D:'s 1.2 TB.
The shared repository is on the smallest data drive on the machine, and it
already holds 36 GB. That is worth a decision before more is moved onto it.

### 5.3 Ask the runtime instead of assuming (medium)

Today the vision provider loads `config.models.vision.model` because that is
what the config says. §8 asks for capability-based selection: *"I need frame
description; what can already do that?"* The provider abstraction is the right
seam — a `preferred` list per capability, resolved against `/api/tags` and
`/api/ps`, choosing an already-loaded compatible model over a stopped one.

Cheap version, high value: **prefer a model that is already resident.** If
`/api/ps` shows a compatible VLM loaded, use it rather than causing a second
load. VAI already reads `/api/ps`.

### 5.4 Ownership and coordination (larger — needs the other projects)

Nothing here can be decided by VAI alone, and §13 says the protocol must fit
every project. What VAI can contribute now: it already has `backend/core/gpu.py`
reading free VRAM and resident models, and it already refuses to start work it
cannot fit. That is the honest half of a registry — the observation half.

The missing half is a lock. Two projects can both read "not loaded" and both
issue a request that loads it twice. Ollama itself serialises the *load*, so
the damage is a queue rather than two copies — which is a real mitigation
worth stating: **with Ollama, the race costs time, not memory.**

---

## 6. Risks and race conditions

1. **The environment gap, live now.** This process sees `OLLAMA_MODELS=D:\Models`
   while the machine says `F:\Models`. Any VAI-launched subprocess inherits
   the wrong value. Cause: `setdefault` filling a blank in an older shell.
2. **Release across projects.** VAI now sends `keep_alive: 0` for its own
   models before rendering. If another project is mid-request against the same
   model, that unload lands under it. Ollama will reload on the next call, so
   the cost is latency, not failure — but it *is* VAI reaching into shared
   state. Worth a rule: release only what we loaded, and only when idle.
3. **A model left resident forever.** Measured: `qwen2.5-coder:7b` held 4.7 GB
   with `expires_at` in the year **2318**. Something on this machine sets an
   unbounded keep-alive. No project can plan around that; a registry would at
   least make the owner visible.
4. **Whisper in-process.** Two projects transcribing concurrently each load
   their own copy, and neither can see the other.
5. **The card is 8 GB.** `qwen3-coder:latest` alone is 18.6 GB — it cannot fit
   and will spill to CPU. Any registry needs to record *what fits*, not just
   what exists.

---

## 7. The interface that would fit

VAI's provider abstraction already has the right shape; what it lacks is a
question about the runtime before the answer is chosen.

```text
discover()            -> what is installed, where, how big
status()              -> what is loaded, how much VRAM, since when
acquire(capability)   -> a handle to something that can do the job,
                         preferring what is already resident
release(handle)       -> only what we acquired, only when nobody else holds it
```

Concretely for this repository, `backend/core/gpu.py` becomes the local half of
that and grows a sibling — say `backend/core/models_registry.py` — that reads
the shared state and answers `acquire`. Nothing above the provider layer
changes: workers keep asking for "a vision provider" and stay unaware that the
answer now depends on what the machine is already doing.

**What should not be built:** a VAI-owned model server, a VAI-managed model
directory, or an auto-download path. All three are §3 violations, and none is
needed — Ollama already is the shared runtime for the two models that matter.

---

## 8. Recommendation

5.1 is done. Do 5.2 next, after deciding whether F:'s 67 GB is the right home
for another 4.4 GB — it is the smallest data drive on the machine and already
holds 36 GB of models.

Hold 5.3 and 5.4 until the other projects on this machine have been inspected.
A protocol invented by one consumer, before its peers are known, is the
project-specific protocol §13 warns against.

**One thing to settle early, because it is already live:** VAI sends
`keep_alive: 0` for its own models before every render (`backend/core/gpu.py`).
On a single-project machine that is correct and was measured fixing a real
render failure. On a shared one it can land under another project's request —
costing a reload, not a failure, since Ollama simply loads it again. Before the
other projects are wired in, that release should learn to ask *"is anyone else
using this?"* rather than *"did we load it?"*. It is the one place this project
currently reaches into shared state.

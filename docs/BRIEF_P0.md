VAI Editorial Engine — Operating Brief (P0 → P1), Final Version
We are continuing the VAI editorial-engine project.
IMPORTANT OPERATING RULES

1. Do not redesign architecture that is already established in PLAN.md / SPEC.md.
2. Do not start multiple phases at once.
3. Finish the current phase completely before starting the next one.
4. Every new field must have a consumer test proving that the field is actually used.
5. Every new invariant must have a regression test that fails when the invariant is removed.
6. Full pytest suite is required before closing a phase. Unit tests alone are not sufficient.
7. Real-footage validation on the 88.5-minute benchmark is part of acceptance.
8. Do not optimize visual style or add new AI models while P0 safety/integrity work is incomplete.
9. Do not commit until the phase acceptance gate passes.
10. Never hide configuration/programming errors behind a silent generic fallback.

BASELINE LOCATION
Use exactly: docs/BASELINE.md Do not create a second BASELINE.md at repository root.
LINT
Run: ruff check . Lint applies to the whole repository, not only to changed files.
CURRENT STATUS
P0.1 + P0.2 are implemented and under final acceptance.
The 88.5-minute benchmark session is the canonical real-footage test.
P0.2 has already demonstrated:

* excluded content is removed at clip level
* excluded moments are rejected
* context does not cross excluded spans
* EDL clips no longer intersect excluded spans
* uncovered gaps can be bridged only when no detector observed anything inside
* the bridge regression test fails if the bridge is removed
* configuration bug involving context.profiles_dir was fixed
* the first render removed menu / mission-failed content
* the first render revealed a loading-screen leak in an uncovered gap
* the bridge fixed that class of leak
* second render is the final acceptance render

DO NOT START P0.3 UNTIL P0.2 IS FORMALLY CLOSED.
================================================== PHASE A — CLOSE P0.2
Before any further implementation:

1. Finish the current acceptance render.
2. Verify the final render on the same 88.5-minute source.
3. Verify automatically that no final-render span belongs to excluded content states:
   * MENU
   * LOADING
   * MISSION_FAILED
   * RESTART
   * PAUSE
   * GAME_INTRO
   * BLACK_SCREEN
   * other configured non-gameplay states above the exclusion floor
4. Visually inspect the critical source interval around 3:43–4:17 and verify:
   * mission failed is absent
   * loading screen is absent
   * restart / intro is absent
   * real gameplay adjacent to the excluded material remains intact
5. Confirm:
   * zero EDL clips intersect an excluded span
   * zero accepted moments have a core inside excluded content
   * context does not cross excluded content
6. Run the BRIDGE SAFETY MEASUREMENT (below) and include its numbers in the acceptance report.
7. Run the full pytest suite.
8. Run `ruff check .`
9. Update:
   * docs/PLAN.md with a P0.2 closure note
   * docs/BASELINE.md only if the frozen baseline genuinely changed
   * README only with the already-agreed P0.2 summary
10. Document explicitly:

* what P0.2 fixed
* what bugs were found during real execution
* what was intentionally deferred

11. Only after all acceptance conditions pass:

* create the P0.2 commit
* push it
* report exact test/render numbers

BRIDGE SAFETY MEASUREMENT
For every excluded source span, inspect a configurable neighboring window (default ±N seconds; document the chosen N and where it is configured).
Measure the amount of observed GAMEPLAY immediately adjacent to the excluded span that is present in:

* the pre-bridge render
* the post-bridge render

Report:

* neighboring gameplay seconds
* retained gameplay seconds
* removed gameplay seconds

The acceptance report must show these numbers. Visual inspection may supplement the measurement but does not replace it.
P0.2 ACCEPTANCE GATE
P0.2 is CLOSED only when all are true:

* final render contains zero excluded non-gameplay spans
* no accidental gameplay loss caused by the bridge (proven by the BRIDGE SAFETY MEASUREMENT numbers, not by inspection alone)
* zero EDL/excluded-span intersections
* regression test proves the bridge is necessary
* full pytest passes
* `ruff check .` passes
* PLAN.md records closure
* baseline movement, if any, is documented in docs/BASELINE.md
* commit is clean and reproducible

Dataset regression is NOT a P0.2 blocker (see DATASET AVAILABILITY EXCEPTION).
================================================== PARALLEL WORK — HUMAN-LABELED GOLDEN DATASET
This work does NOT modify the pipeline.
HUMAN-LABELED GOLDEN DATASET
The dataset labels are human-authored.
Claude Code must NOT infer, generate, or fill the labels from pipeline output.
Claude Code may create:

* directory structure
* CSV schema
* empty CSV files
* validation tooling
* scoring script
* labeling instructions

The human manually enters: start,end,label,note
If a CSV contains no human labels, score_moments.py must report: "No labeled spans available; baseline cannot be computed" and must not invent or estimate a baseline.
Sessions (3):

1. the canonical 88.5-minute session
2. a second game
3. a slower / calmer gameplay session

Location: tests/golden/labels/<project>.csv
Schema: start,end,label,note
Use ONLY these labels:

* best_moment
* unimportant
* event_start
* payoff
* reaction
* dead_time
* failed_attempt
* non_gameplay

Labeling rules (for the human):

* labels are spans, not individual frames
* minimum span = 2 seconds
* if uncertain for more than 5 seconds, use note and do not force a label
* target approximately 60–100 spans/session
* do not create more labels than necessary

Scoring script: scripts/score_moments.py
It must report:

* Precision
* Recall
* F1 for best_moment
* Boundary Error for event_start
* non_gameplay leakage rate

The first human-labeled measurement becomes the baseline. Record the baseline numerically in docs/BASELINE.md.
DATASET GATE
scripts/score_moments.py must always print a comparison report when a baseline is available.
Normal execution:

* prints current metrics
* prints delta vs docs/BASELINE.md
* exits 0 even when metrics regress

Gate execution:

* use an explicit gate/compare mode
* exits non-zero only when a configured threshold is violated

Default thresholds:

* best_moment F1 may not decrease by more than 0.02
* non_gameplay leakage may never increase
* event-start Boundary Error may not increase by more than 0.50 seconds

These thresholds are the acceptance policy unless PLAN.md explicitly defines stricter thresholds.
DATASET AVAILABILITY EXCEPTION
The golden dataset is a parallel measurement track.
Before the first human-labeled baseline exists, dataset regression is "NOT YET AVAILABLE" and is not an acceptance blocker for P0.2.
After the first baseline is recorded, dataset regression becomes a required gate for P0.3+.
RULE: No new perception signal is accepted after P0 unless it demonstrates measurable improvement on this dataset under the DATASET GATE thresholds:

* F1 must not decrease
* Boundary Error must improve or remain acceptable
* non_gameplay leakage must not increase

================================================== PHASE B — P0.3 AUTHORIZED SPAN
START ONLY AFTER P0.2 IS CLOSED.
Before coding:

1. Read docs/PLAN.md
2. Read docs/SPEC.md
3. Read backend/gaming/content.py
4. Inspect the current EDL/clip-building path
5. Identify the exact existing data structures used by the pipeline
6. Do not invent a parallel architecture if an existing structure can carry the contract

Goal: Introduce the AuthorizedSpan contract exactly as specified by the existing project design.
Core invariant: Every planned clip must have an explicitly authorized source span.
For every planned clip, record:

* source_start
* source_end
* granted_by
* reason

Rules:

1. A clip may narrow an authorized span.
2. A clip may NOT widen an authorized span. Widening requires a NEW AuthorizedSpan whose granted_by belongs to the closed allowlist (see AUTHORIZED SPAN IDENTITY).
3. Style logic may not widen an authorized span.
4. Critic logic may not widen an authorized span.
5. Jump-cut / context expansion may not silently widen an authorized span.
6. The final EDL must be validated against authorization, not merely against the original input.
7. Unauthorized widening must be a hard failure, not a warning.

AUTHORIZED SPAN IDENTITY
granted_by is a CLOSED set.
The allowed grant identities must be derived from PLAN.md / SPEC.md and implemented as an explicit enum or equivalent closed validation mechanism.
A new granter may NOT be introduced by:

* style
* critic
* jump-cut logic
* arbitrary caller code

An authorization expansion is represented by a NEW AuthorizedSpan. The original AuthorizedSpan is immutable with respect to its authorization bounds.
Narrowing an existing AuthorizedSpan is allowed. Widening requires creation of a new explicitly authorized span whose granted_by value belongs to the closed allowlist.
Tests must include an attempted unauthorized granter injection and must fail.
Important: P0.3 is about SAFETY OF SOURCE BOUNDS. It is NOT about deciding whether a moment is good.
Do not mix into P0.3:

* style quality
* ranking changes
* new perception models
* new effects
* hook quality
* duration optimization

Required tests:

1. unit test: normal authorized clip passes
2. unit test: narrowing passes
3. unit test: unauthorized widening fails
4. unit test: style cannot widen
5. unit test: critic cannot widen
6. unit test: unauthorized granter injection fails
7. integration test on the 88.5-minute benchmark
8. render-level regression proving no final clip exceeds its authorization
9. mutation/regression test that deliberately breaks the authorization check and verifies failure

Acceptance gate:

* invariants pass
* regression passes
* DATASET GATE passes (gate mode, default thresholds)
* full pytest passes
* `ruff check .` passes
* PLAN.md closure note exists
* commit only after acceptance

================================================== PHASE C — P0.4 HOOK / ENDING GATES
Start only after P0.3 is closed.
Implement the existing project design for:

* exactly one HOOK at the front
* exactly one ENDING at the back
* explicit exceptions only where policy allows

Ending must reject:

* unresolved combat
* failed attempt tail
* non-gameplay final shot
* unresolved editorial state

ENDING EVIDENCE PRIORITY
Primary acceptance evidence:

1. sufficient resolution evidence
2. reaction following the final meaningful event

Secondary evidence: 3. descending semantic intensity
Semantic intensity must not be the sole reason for accepting an ending.
Because semantic calibration is finalized in P0.7, P0.7 acceptance must rerun all relevant P0.4 ending tests and confirm that they still pass.
Tests must include the real benchmark ending.
Do not add fancy hook effects yet. The goal is STRUCTURAL HOOK/ENDING correctness.
Acceptance gate: same structure as P0.3 (invariants, regression, DATASET GATE, full pytest, `ruff check .`, PLAN.md closure note, commit only after acceptance).
================================================== PHASE D — P0.5 MISSION / ATTEMPT
Start only after P0.4 is closed.
Use existing:

* Media/session information
* Situation
* Episode
* Link
* content/gameplay state

Add only the minimum derived concepts explicitly required by PLAN.md:

* Mission / Chapter
* Attempt

Do not create redundant parallel concepts if an existing structure already expresses part of the relationship.
ATTEMPT DISPOSITIONS
The allowed dispositions are exactly those defined in PLAN.md.
Do not introduce additional disposition values unless PLAN.md is explicitly updated first.
The benchmark must demonstrate that repeated failed attempts are understood as related attempts rather than independent unrelated events.
Tests must validate:

* attempt start
* attempt end
* failed attempt detection
* relationship between attempts
* final selection behavior

Acceptance gate: same structure as P0.3.
================================================== PHASE E — P0.6 JUMP-CUT DECISION + WORD TIMESTAMPS
Start only after P0.5 is closed.
Change the jump-cut question from: "Can this gap be removed?"
to a context-aware decision using:

* event importance
* dead-time evidence
* spoken-word boundaries
* reaction boundaries
* sequence/jump density budget

Jump cuts are allowed only when:

1. the removed region is genuinely dead
2. the gap is long enough to matter
3. it is not inside a spoken word
4. it does not remove an event onset
5. it does not remove a reaction
6. the sequence has not already exceeded its jump-cut density budget

WORD-LEVEL TIMESTAMPS
This is NOT a new perception model if implemented by enabling word-level timestamps in the existing transcription stack.
Do not introduce WhisperX or another transcription dependency merely to satisfy this requirement unless PLAN.md explicitly requires it.
If the existing transcription output changes, account for the resulting stage digest/cache invalidation explicitly. Recompute only the affected downstream stages as required.
P0.6 must document the impact on transcript/semantic cache identity before changing persisted outputs.
Use word-level timing for:

* "do not cut inside a word"
* cleaner speech boundaries
* accurate speech-aware jump cuts

Measure the previously blind spans explicitly.
Acceptance must include:

* test that cutting inside a word fails
* test that a clean word boundary passes
* real-footage regression
* measured reduction in speech-boundary blindness
* no regression in existing pacing behavior
* documented cache/digest impact and the recompute performed
* DATASET GATE, full pytest, `ruff check .`, PLAN.md closure note

================================================== PHASE F — P0.7 GAMEPLAY LEVEL CALIBRATION
Start only after P0.6 is closed.
Fix semantic level calibration so genre-specific footage is not incorrectly classified as sustained climax/high intensity.
Use the existing semantic lanes and event evidence.
Do not solve this by simply changing arbitrary thresholds until the real benchmark looks better.
Create measurable checks around:

* calm
* normal gameplay
* high
* climax

The benchmark must show that:

* stealth / slow gameplay is not incorrectly treated as climax
* true escalation is still recognized
* event density does not become the only proxy for excitement

Acceptance requires:

* regression + golden dataset comparison (DATASET GATE)
* all relevant P0.4 ending tests rerun and passing (see ENDING EVIDENCE PRIORITY)
* full pytest, `ruff check .`, PLAN.md closure note

================================================== PHASE G — P0.8 DURATION OPTIMIZATION + FINAL AUDIO QA
This is intentionally LAST.
Do not optimize target duration before all earlier safety constraints are closed.
Current known behavior: removing poor content can cause the optimizer to over-represent weaker surviving material because the duration target is hard.
P0.8 must invert the duration constraint so that: QUALITY is primary and TARGET DURATION is secondary.
DURATION CONSTRAINT
The existing output policy remains authoritative.
The optimizer may produce a shorter video than the target duration when quality would otherwise be degraded, but it may not violate the hard minimum or maximum duration defined by output.yaml.
P0.8 must distinguish:

* target duration
* quality floor
* hard output bounds

The duration constraint must never override safety invariants.
Acceptance must compare:

* selected quality
* redundancy
* weak-content share
* final duration
* story coherence

Do not "solve" duration simply by relaxing safety constraints.
AUDIO QA — PART OF P0 ACCEPTANCE
Final-render audio QA belongs to P0, not a later polish phase. It is integrated in P0.8 / final acceptance.
Minimum checks:

1. overall loudness
2. no clipped audio
3. no abrupt audio discontinuity at cuts
4. reactions marked important remain audible
5. music/game ducking does not mask important speech/reaction
6. final audio remains within configured loudness policy

AUDIO QA EVIDENCE
Final-render audio QA uses two evidence sources:
A. Pipeline evidence: reaction spans already identified by the editorial/moment pipeline. This is the production QA path and applies to every project.
B. Golden dataset reaction labels: used only for evaluation/regression measurement on the labeled benchmark.
The final QA must verify that pipeline-identified important reactions remain audible after rendering and are not masked by ducking/music.
Do not introduce decorative audio features yet. This is QA, not audio embellishment.
Acceptance gate: same structure as P0.3, plus the audio QA checks above passing on the 88.5-minute benchmark render.
================================================== AFTER P0 — P1 PERCEPTION
Only after ALL P0 phases are closed.
Before adding any new model: run the golden dataset baseline.
Then test ONE perception upgrade at a time.
Experiment 1: vision model candidate (e.g. Qwen3-VL or another viable model)
First measure:

* RTX 3070 VRAM usage
* number of frames per request
* inference latency
* throughput
* context limits
* interaction with Whisper/other models
* quality metrics on golden dataset

Do not adopt the model because it "looks smarter".
Adopt only if:

* it is operationally feasible
* F1 improves or boundary error improves
* non-gameplay leakage does not worsen

Experiment 2: emotion/laughter channel
Evaluate SenseVoice / Whisper-AT / equivalent against the same dataset.
Experiment 3: human A/B preference collection
Build preference collection first. Do NOT build a learned re-ranker immediately.
Collect at least 100 comparisons before training a re-ranker.
================================================== AFTER P1 — P2 NEW SIGNALS
Potential candidates:

* facecam lane
* visual embeddings
* game-specific HUD detectors
* additional game profile detectors
* chat replay signal

No signal is accepted automatically.
Every signal must:

1. have a defined consumer
2. have tests
3. be measurable
4. improve F1 / boundary quality / selection quality
5. have a fallback behavior
6. not weaken deterministic safety constraints

================================================== P3 — EDITORIAL POLISH
Only after the core editor is validated.
Potential later work:

* dynamic punch-ins
* speed ramps
* impact SFX
* freeze frames
* better captions
* chapters
* Shorts reframing
* advanced visual effects

These are polish. They must not be used to disguise weak perception or weak editorial selection.
================================================== GLOBAL ARCHITECTURE RULES
VAI follows this control flow:
Evidence ↓ Perception ↓ Structured analysis ↓ Editorial reasoning ↓ Validated decisions ↓ Deterministic execution ↓ Critic / QA ↓ Render
LLMs/models reason and propose. Code validates. Deterministic systems execute. QA verifies.
Never allow an LLM to directly control FFmpeg/shell/render operations.
Every new field:

* must have a consumer
* must have a test
* must be observable in logs/debug output when useful

Every safety rule:

* must have a regression test
* must be validated on real footage

Every phase:

* must have a measurable acceptance gate
* must pass the DATASET GATE once a baseline exists (see DATASET AVAILABILITY EXCEPTION for P0.2)
* must update PLAN.md
* must remain reproducible
* must not silently swallow configuration errors

================================================== CURRENT COMMAND
Do NOT start implementing P0.3 yet.
Finish P0.2 acceptance first.
When P0.2 is fully accepted:

1. commit
2. push
3. report the exact acceptance numbers (tests, render, bridge safety measurement)
4. stop

Then wait for explicit authorization to start P0.3.
Do not combine P0.2 and P0.3 in the same implementation pass.

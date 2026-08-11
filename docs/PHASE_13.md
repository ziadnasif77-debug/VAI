# Phase 13 — The model, arriving last

SPEC §63, §85, §92–§95; §126 step 19. **Acceptance: an instruction the rule
parser cannot read still changes the video — and a machine with no model still
works.**

Status: **complete and verified**, both halves: the wiring against a scripted
model in the suite, and the criterion itself against a real `qwen2.5:7b-instruct`
running locally.

---

## Delivered

| Requirement | Where | Verified by |
| --- | --- | --- |
| Structured local completions (§93, §94) | `ai/llm/ollama_provider.py` | `test_llm_provider.py` — 49 tests |
| A deterministic model for the suite (§113) | `ai/llm/fake_provider.py` | Used by everything below |
| Versioned prompts (§92) | `prompts/interaction/{instruction,command,question}/` | `TestThePrompts` |
| Reading unparsed text (§63, §85) | `backend/interaction/llm_fallback.py` | `test_llm_fallback.py` — 26 tests |
| The wiring, end to end | `backend/interaction/service.py`, `qa.py` | `test_llm_editing.py` — 18 tests |
| Real-model check | `scripts/verify_phase13.py` | Run against Ollama — see below |

---

## Why the model arrives fourteenth

Everything a model *could* have decided in this pipeline is decided by rules
instead. Which moments matter is ten weighted dimensions (§32). How long the
video is is a constrained optimisation (§39). What order the clips go in is a
narrative arc built from scored moments (§35). All of it can be read, tested,
and argued with, and none of it changes its mind between runs.

What was left is the one thing rules are genuinely bad at: reading a sentence
someone typed. That is this phase, and it is deliberately the whole of it.

The direction of the fallback follows from that. The rule parser runs **first**
and the model only sees what it could not read — so a machine without Ollama
loses the unusual phrasings, not the feature (§95). Reversing the order would
have made the model load-bearing for "delete clip 5".

---

## The model reads; it does not decide

`LlmInterpreter` returns a `Reading`, and a `Reading` holds either one of the
same three objects the rest of the system already knows — `IntentDelta`,
`EditCommand`, `Answer` — or nothing, plus the reason why.

There is no path from a sentence to an effect that skips the validation a typed
command already goes through (§85). What comes back from the model is validated
by the same Pydantic models, applied by the same service methods, and recorded
in the same version history. `_handle_command` calls `apply_command` — the
identical entry point the API uses for a command a person typed into a form.

Three consequences, each enforced rather than trusted:

**It never touches a file.** §63 is explicit: natural language modifies project
state, not files. Nothing in `llm_fallback.py` opens, writes, or deletes
anything — it returns data structures.
`test_no_file_is_touched` compares the project directory before and after two
model-driven edits.

**A refusal is a real answer.** A model that cannot express an instruction says
so in `unsupported`, and the person is told. Silently applying the nearest
available preference would leave them believing their instruction was followed —
which is worse than a refusal, because they would not know to try again.

**Below 0.5 confidence, nothing is applied.** A model that is unsure has
guessed, and a guessed edit costs more than a clarifying question.

---

## A prompt is guidance; the check is in the code

A clip index must exist. The prompt says so — "the video currently has
{clip_count} clips" — and the model may still name clip 99, so
`1 <= index <= clip_count` is re-checked before anything is applied, and
refused by name: *there is no clip 99 in this edit.*

The same test-writing exercise found that the model's path skipped the duration
check the rule path goes through. That guard now exists too — but the real
model then made the point far better than the test did, and the answer turned
out not to be a guard at all. See below.

---

## The model does not set the duration

Ollama enforces a JSON schema as a **grammar**: the model physically cannot emit
a token sequence the schema forbids. `target_duration_seconds` was declared
`minimum: 600`, so "make it 30 seconds" could not produce 30.

It produced **3000**. The person asked for thirty seconds and was told their
video was now fifty minutes. Asked for "25 minutes" it produced 2500 — 41
minutes. The band check passed every time, because every answer was inside the
band; the constraint that was supposed to protect the value was what corrupted
it.

Adding a prompt rule ("if they ask for a length outside the band, refuse")
changed nothing across repeated runs. So the field came out of both schemas
entirely, and `set_duration` came out of the command enum:

> A duration is arithmetic. The rule parser reads "make it 25 minutes",
> "25 min" and "٢٥ دقيقة" exactly, the import screen has a picker (§6), and a
> 7B model at q4 does not. There was never anything to gain here.

The band check in `llm_fallback.py` stays as the guard for a prompt that
reintroduces one, and its test says why.

---

## Four values that never existed

Running the real model, every reading of "give it the feel of a wildlife
documentary" failed with *the model's answer did not fit the editing
preferences*. The model was not at fault:

| Schema offered | Actually exists |
| --- | --- |
| `dead_time_policy: keep_context` | `keep` |
| `captions: animated` | `important_only`, `full` |
| `pacing` without `very_fast` | four levels, not three |
| `context_preservation`, `variety` without `none` | four levels, not three |

The prompts were written by hand against my memory of the enums. Because the
schema is a grammar, the model was *forced* to emit `keep_context` and then
rejected for it — so the failure was total and permanent, not occasional, and
the only symptom was a polite refusal that blamed the model.

`test_every_choice_offered_is_a_choice_that_exists` compares the schema's enums
to the real ones value by value, and `test_the_prompt_text_names_the_same_choices_as_the_schema`
checks the prose explains every value the schema allows. The original test
compared *field names* and passed throughout.

---

## Grounding is resolved, not requested

Asking a model to cite its sources produces citations. It does not produce
*true* citations.

So the question path gives the model records with ids, and every id it cites is
resolved back to the record it names. An invented id does not resolve. An answer
standing entirely on invented citations therefore arrives with **no evidence**,
and is refused by the same §80 rule every other claim in this system obeys: a
claim without evidence is not shown.

The pool itself is bounded by `interaction.llm_fallback.max_input_records` (60),
and comes from the same knowledge base the deterministic resolvers read — so an
LLM answer and a database answer are grounded in the same rows.

---

## Two-step escalation, and the classifier it works around

`classify()` routes a message to the command handler only when it carries a
delete/restore verb **and** names a clip or a timestamp. That conservatism
predates this phase and was right: without a model, the alternative was rules
deleting footage on a vague phrase, so anything vaguer went to the instruction
path where it changes intent — reversible — instead of the EDL.

"Delete the part right after the opener" names neither a clip nor a timestamp.
It lands on the instruction path, where the model is asked which *editing
preferences* it changes, correctly answers "none of them", and the person gets
a polite non-answer to a perfectly clear instruction.

So when the instruction path finds nothing, the command prompt is tried before
giving up. The order is the point: **preference first, then edit** — the safe
reading before the destructive one, and the destructive one still validated
against a real clip count. The cost is one extra local call on a message that
has already failed everything else.

---

## Reject, retry, then fail (§94)

A local model asked for JSON will occasionally answer with an apology, a code
fence, or a JSON array of answers. `MAX_ATTEMPTS = 3` — two retries, because a
model that fails a schema twice will not succeed on the fifth, and every caller
has a §95 fallback to reach for.

The schema shipped beside the prompt (§92) is given to Ollama as its `format`
parameter **and** checked on the way back: one definition, enforced at both
ends. The check on return is deliberately shallow — the object is a mapping with
the keys the schema requires — because full validation is the caller's Pydantic
model, which knows what the values *mean*. Duplicating that here would give the
rules two places to drift apart.

---

## Without a model

| What happens | Result |
| --- | --- |
| Ollama not running | `is_available()` false; rules answer, model never asked |
| Ollama running, model not pulled | Also unavailable — checked via `/api/tags`, so the failure lands at the health check rather than minutes into an interaction |
| `llm_fallback.enabled: false` | Never consulted at all |
| Model dies mid-session | The message stands, the cached availability is cleared, and the next message re-checks |

In every case the rule path is untouched: `focus on the funny moments` and
`delete clip 2` keep working, and `TestWithoutAModel::test_the_rule_path_still_works`
asserts the model was never even asked.

---

## Acceptance

### In the suite, with a scripted model

| Claim | Test |
| --- | --- |
| The chosen sentences really are unreadable by the rules | `TestTheRulesReallyCannotReadThese` |
| An unparsed instruction changes the stored brief | `test_an_unparsed_instruction_changes_the_editing_brief` |
| An unparsed command shortens the real edit | `test_an_unparsed_command_changes_the_edit` |
| It is as undoable as any other edit (§42) | `test_the_command_is_recorded_as_a_version` |
| No analysis job is re-queued (§10, §127) | `test_the_analysis_is_not_re_run` |
| No file is written (§63) | `test_no_file_is_touched` |
| With no model, nothing breaks and nothing changes (§95) | `TestWithoutAModel` — 4 tests |

### Against the real model

`scripts/verify_phase13.py`, `qwen2.5:7b-instruct` (q4_K_M) on Ollama, six
moments and six clips totalling 20:00:

| Typed | Rules | Result |
| --- | --- | --- |
| give it the feel of a wildlife documentary | unreadable | pacing → medium, dead time → balanced, context → high, effects → minimal |
| make it punchier, more like a highlight reel | unreadable | pacing → fast, dead time → aggressive |
| **get rid of whichever clip is the weakest** | unreadable | **removed clip 5 — the edit went 20:00 → 16:42** |
| make it 30 seconds | unreadable | refused |
| grade it teal and orange | unreadable | refused |
| did I sound frustrated at any point? | unreadable | refused — the model's answer cited nothing in the analysis |
| delete the part right after the opener | unreadable | refused as ambiguous between a clip and a range |

The third row is the acceptance criterion: a sentence naming no clip and no
timestamp, which the rules report zero confidence on, became a validated
`delete_clip` and shortened the video. And it picked the right clip — 5 of 6,
the lowest-scored.

The last row is the model declining rather than guessing, which the command
prompt asks for explicitly ("a wrong edit costs more than a clarifying
question"). Both refusals are the system working.

---

## Bugs found while building this

| Defect | Consequence had it shipped |
| --- | --- |
| **Four enum values in the prompts do not exist** | Every reading of four of the eight dimensions rejected, permanently, blaming the model |
| **The schema forced an invented duration** | "Make it 30 seconds" silently became a 50-minute video; "25 minutes" became 41 |
| A model reading that changes nothing reported success | "Updated the editing brief" when the brief was identical — and the command escalation never got its turn |
| The LLM path skipped the duration band check | A 30-second target written past a `CHECK` constraint, a config validator and the EDL builder |
| An edit command that names no clip lands on the instruction path | The model answers "unsupported" and a clear instruction gets a polite non-answer |
| The service and its Q&A each built their own `LlmInterpreter` | Two availability checks and two providers per message, on a machine with one GPU (§54) |
| An edit version recorded the command *kind* as its reason | "delete_clip" — for a sentence the model read, the only record outside the chat log |
| The model's sentences dropped into ours unedited | "I read that, but The instruction ... preferences.." — reads like a bug when the answer is right |

The top two were found **only** by running the real model, and neither was
reachable through a fake answering what the test author expected. That is worth
recording as a method, not just a result: a scripted provider tests the code
around the model, and nothing else. `scripts/verify_phase13.py` exists so the
other half stays checkable.

---

## Not built, and why

| Deferred | Why |
| --- | --- |
| A model-written *narrative* (§35) | The arc is built from scored moments and is testable. Handing it to a model would trade a property this project can verify for one it cannot |
| Streaming replies | The chat panel polls, and a two-second local completion does not need tokens arriving one at a time |
| Conversation memory in the prompt | Instructions already accumulate as an ordered delta log (§11), which is a better memory than a transcript: it is what the pipeline actually reads |
| Retrying with a repair prompt | §94 asks for reject-and-retry, and the same prompt at temperature 0.1 is the honest version of that. "Here is what you got wrong" is a different feature |

---

## Gate to Phase 14

Met: a sentence the rules cannot read changes the video, through the same
validated command layer as everything else — and a machine without a model
still makes videos.

Phase 14 is game profiles (§111): one real game end to end, then a profile API.

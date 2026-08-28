# DIRECTION — the owner's editing doctrine

> Delivered by the owner on 2026-08-28 as the master prompt for the AI
> editor. This file is its canonical home. It does not replace SPEC.md —
> SPEC says what the system *is*; this says what the finished video must
> *feel like*, and every editing subsystem answers to it.
>
> How it lands in practice: VAI's architecture keeps the model as the
> proposer and deterministic code as the decider, so most of these rules are
> enforced in code (budgets, cooldowns, guards, §51's asking, §39's clock)
> rather than pasted into a 7B prompt that could not hold 34 sections. The
> Director and Critic prompts carry the doctrine's distilled decision
> filter; the gap table at the bottom tracks what is not yet built.

## The decision filter (every edit answers at least one)

1. Does it increase viewer retention?
2. Does it improve understanding?
3. Does it increase emotional impact?
4. Does it emphasize an important gameplay event?
5. Does it improve pacing?
6. Does it strengthen the narrative?

If none apply, the edit does not happen. The best edit is not the edit with
the most effects; every decision has a purpose, or it is noise.

## The grammar

```
SETUP → BUILDUP → TENSION → PAYOFF → REACTION → NEXT MOMENT
```

never

```
MOMENT → RANDOM EFFECT → MOMENT → RANDOM EFFECT
```

The edit breathes. Not every highlight gets an effect; not every kill gets a
zoom; escalation is built (kill 1 plain → kill 4 heavy), and repeated
extremes cancel themselves.

## Standing rules already carried in code

| Doctrine section | Where it lives |
| --- | --- |
| Never invent events; UNCERTAIN over hallucination | §21/§80 evidence chain; the Director's index guardrail; creative-text validation |
| Understand before editing (timeline of intensity) | the full analysis pipeline; episodes (Phase B); §46 |
| Cut menus/loading/dead time/recorder chrome | dead-time (§36), frame states (§77), screen guard + recorder probe |
| Effects with reason, target, budget, no stacking wars | effects engine: triggers, budgets (6/min), realiser registry, §69 |
| Hook first, intro ≤3 s, outro short | narrative hook config (§37), story structure |
| Audio ducked, dialogue intelligible, no invented music | §72–§73 mix; "no local music found" note |
| Text sparingly, never over UI | caption engine collision rules; effects text guards |
| Self-critique before finalising | the Critic (Phase E) with apply-and-veto |
| Thumbnail = advertisement, ≤5 words, readable small | metadata/hooks.py style + research-backed strokes and scrim |
| Title from the strongest real event, no fake claims | metadata/generation + creative writer with evidence-only prompt |
| Capability awareness (never request unsupported effects) | realisable-set registry; §92 prompt/version registry |
| EDIT_PLAN as machine-readable output | the EDL (§40–§42) — it *is* the edit plan |
| Named highlight tiers on every score | moments/scoring.py `tier_for` — master/major/good/supporting on the ten-dimension score; a second scoring system beside the first would only raise which one is real |
| Quality score 0–100 + uncertainty list | qa_worker `_doctrine_summary` — arithmetic over failures, warnings and every §95 note upstream stages attached, plus those notes verbatim |

## Gap table (doctrine asked, not yet built)

| Gap | Doctrine § | Note |
| --- | --- | --- |
| Pacing tiers driving cut length (LOW/MED/HIGH/PEAK) | 7 | pacing exists per §38; intensity-tiered cut-length policy is coarser than the doctrine's |
| Explicit escalation ladders and per-effect cooldowns | 9 | budgets cap totals; a declared cooldown/escalation curve per effect type is not modelled |
| Speed ramps (variable speed inside one clip) | 11 | clip-level speed exists; in-clip ramp curves do not |
| Freeze frames | 12 | not in the effect library |
| SFX layer (whoosh/riser/hit/bass) | 14 | no licensed SFX assets shipped; the mix carries none by design until assets exist |
| Music intensity mapping | 15 | §73 plays what the person provides; no intensity-matched selection |
| Thumbnail composition plan (subject crop/arrows/spotlight) | 22–23 | current: peak frame + styled text; no subject extraction or composition planning |

Each gap graduates from this table by shipping with tests and a PLAN entry,
in the order the owner's feedback pulls them.

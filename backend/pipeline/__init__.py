"""Pipeline orchestration (SPEC §46, §47).

Stage workers claim jobs from the job manager, run one stage, and report back.
Keeping state in the database rather than in a process is what makes resume
after a crash possible. Workers land with their stages, from Phase 2.
"""

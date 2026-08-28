-- Two per-project choices made at the import screen (2026-08-28).
--
-- captions_enabled: whether the transcript is written INTO the frame as
-- captions. New projects default to 0 -- the person opts in to text on the
-- video -- while every project that already exists was created when captions
-- were unconditional, and keeps the behaviour it was made with.
--
-- output_directory: where the finished video is copied when a render
-- succeeds. NULL means what it always meant: the file stays in the project's
-- renders/ directory and nothing is copied anywhere.

-- auto_publish: when 1, a successful QA queues the YouTube publish on its
-- own, with metadata generated from the analysis. The §51 rule stands --
-- nothing is delivered *unasked* -- and this flag IS the asking, made once,
-- explicitly, per project, at the import screen.

ALTER TABLE projects ADD COLUMN captions_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN output_directory TEXT;
ALTER TABLE projects ADD COLUMN auto_publish INTEGER NOT NULL DEFAULT 0;

UPDATE projects SET captions_enabled = 1;

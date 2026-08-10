-- On-screen text (SPEC sections 25, 23, 45).
--
-- §25 is explicit: "Every OCR result must have a timestamp." The column is
-- NOT NULL for that reason -- text without a time cannot become an event, and
-- a nullable column would let one in.
--
-- `region` records where the text came from: a named profile region, or
-- 'full_frame' for the unknown-game path (§23). That distinction is most of a
-- detection's meaning -- "ELIMINATED" in the kill feed is evidence, the same
-- word somewhere on screen is a maybe -- so it is stored rather than inferred.

CREATE TABLE ocr_results (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    media_id      TEXT NOT NULL REFERENCES media (id) ON DELETE CASCADE,

    timestamp     REAL NOT NULL,
    text          TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 0.0,

    -- Named profile region, or 'full_frame'.
    region        TEXT,
    -- Bounding box in the analysed image, JSON [left, top, right, bottom].
    box           TEXT,

    -- Which profile was in force, so a reading is attributable to the layout
    -- it was read against (§49).
    game_profile  TEXT,
    engine        TEXT,

    created_at    TEXT NOT NULL,

    CHECK (timestamp >= 0),
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

CREATE INDEX idx_ocr_media_time ON ocr_results (media_id, timestamp);
CREATE INDEX idx_ocr_project ON ocr_results (project_id);

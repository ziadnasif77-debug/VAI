"""Quality measurement (SPEC §112, §117–§119).

The last thing this project builds, and the first that can say whether any of
it is any good. Everything before it measured behaviour — memory stayed flat,
the render decoded, five broken files were each caught by name. None of that
says whether the moments it picks are the moments a person wanted.

Three modules, matching the three sections:

* :mod:`backend.quality.dataset` — the benchmark itself (§117): real gameplay,
  annotated by a person who was not looking at the system's output.
* :mod:`backend.quality.metrics` — precision, recall and the rest (§118),
  computed by matching predictions against those labels in time.
* :mod:`backend.quality.user_edits` — what the person did to the edit (§119),
  read from the versions the interaction layer already records.

The distinction between the first two and the third is worth keeping: a golden
dataset is a fixed opinion held once, and user edits are a continuous one. The
second is the more honest signal and the harder one to collect, which is why
§119 exists separately from §118.
"""

from __future__ import annotations

__all__: list[str] = []

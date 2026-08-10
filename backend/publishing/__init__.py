"""Distribution layer -- where a finished render goes.

This package exists so that adding a destination never reaches back into the
pipeline. It sits after QA and depends only on the render output and the
publication models; the analysis, moment, narrative, timeline and rendering
layers do not import it and do not know it exists.

Current state:

* ``local_file`` -- implemented. Copies the QA-passed render to a user-chosen
  path. This is the MVP's export.
* ``youtube`` -- planned, not implemented. Its slot exists in the job graph,
  the database and the configuration; requesting it today raises a typed
  ``PUBLISH_TARGET_NOT_CONFIGURED`` error rather than failing obscurely.

Adding YouTube auto-publish later means: implement a ``Publisher`` here,
register it, and fill in ``config/publishing.yaml``. No pipeline change.
"""

from backend.publishing.base import Publisher, PublisherRegistry, registry
from backend.publishing.local_file import LocalFilePublisher

__all__ = ["LocalFilePublisher", "Publisher", "PublisherRegistry", "registry"]

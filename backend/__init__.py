"""Backend package for the local AI long-form YouTube video editor.

Layering contract (MASTER SPEC section 96)::

    api  ->  services  ->  ai / media / rendering / validation  ->  core models
                                                                ->  database / config / core

Modules must not import "upwards": ``core`` and ``database`` never import from
``services`` or ``api``, ``rendering`` never imports from ``ai``, and the EDL
model never imports a rendering engine.
"""

from backend.core.versions import APPLICATION_VERSION

__version__ = APPLICATION_VERSION

__all__ = ["__version__"]

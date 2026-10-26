"""Slicer Studio: the streamlined default interface of this Slicer fork.

Modules are imported lazily: importing the package does not import the shell. Other modules may
only use :mod:`StudioLib.shell` (``instance()`` and the shell API) and :mod:`StudioLib.ui`.
"""


class _LazyNumpy:
    """Stands for ``numpy`` and imports it on first use.

    Modules imported at startup use it instead of ``import numpy as np``: Studio does not need numpy to start,
    and its first import costs about 0.2 s. Once imported, the attributes are those of the module.
    """

    __slots__ = ()

    def __getattr__(self, name):
        import numpy

        return getattr(numpy, name)


#: ``from StudioLib import lazyNumpy as np`` (see ``_LazyNumpy``)
lazyNumpy = _LazyNumpy()

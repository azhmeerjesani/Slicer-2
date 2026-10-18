"""Slicer Studio: the streamlined default interface of this Slicer fork.

Modules are imported lazily: importing the package does not import the shell. Other modules may
only use :mod:`StudioLib.shell` (``instance()`` and the shell API) and :mod:`StudioLib.ui`.
"""

"""PySide6 widgets for the Nexus Collection Manager.

The GUI is a thin orchestrator: it never reimplements the modding pipeline, it
launches the ``nexus_core`` backends as ``python -m`` subprocesses (see
``gui.runner``) and streams their stdout into the window.
"""

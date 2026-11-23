"""Flatpak / OSTree side of Wildcat Hub.

Replaces ``app.fdroid`` for everything that touches the published
repository: bundle parsing, GPG signing, the OSTree object store and the
per-audience repo variants served to ``flatpak`` clients.
"""

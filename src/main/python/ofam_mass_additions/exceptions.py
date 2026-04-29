"""Exceptions raised by ofam_mass_additions.

Kept local to this package so the runner can be deployed standalone
(no dependency on ofam_asset_xfer).
"""

from __future__ import annotations


class OFAMMassAdditionsError(Exception):
    """Base exception for ofam_mass_additions."""


class ConfigError(OFAMMassAdditionsError):
    """Raised when the user-supplied configuration is invalid or incomplete."""


class FusionApiError(OFAMMassAdditionsError):
    """Raised when an Oracle Fusion API call fails or returns malformed data."""

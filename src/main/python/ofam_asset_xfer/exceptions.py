from __future__ import annotations


class OFAMAssetXferError(Exception):
    """Base exception for the asset transfer pipeline."""


class ConfigError(OFAMAssetXferError):
    """Configuration is invalid."""


class FusionApiError(OFAMAssetXferError):
    """Fusion returned an error or an unexpected response."""


class ValidationError(OFAMAssetXferError):
    """Business/data validation failed."""

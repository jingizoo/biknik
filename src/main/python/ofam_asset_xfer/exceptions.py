from __future__ import annotations


class OFAMAssetXferError(Exception):
    """Base exception for the asset transfer pipeline."""


class ConfigError(OFAMAssetXferError):
    """Configuration is invalid."""


class FusionApiError(OFAMAssetXferError):
    """Fusion returned an error or an unexpected response."""


class ValidationError(OFAMAssetXferError):
    """Business/data validation failed."""


class CMDBApiError(OFAMAssetXferError):
    """CMDB API returned an error or an unexpected response."""


class LocationResolutionError(OFAMAssetXferError):
    """Failed to resolve a CMDB location code to an FA location/book."""

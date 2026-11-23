"""Source fetchers. ``fetcher_for(spec, ...)`` picks the only adapter allowed for the source's access method."""
from .api_fetcher import ApiFetcher, LiveLookupUnavailable, LiveResult
from .base import BaseFetcher, FetchError, FetchOutcome, HostRateLimiter, MissingCredentialError, safe_extract, sniff_mime
from .dropzone_intake import DropZoneIntake, Entitlement
from .git_fetcher import GitFetcher
from .http_archive_fetcher import HttpArchiveFetcher
from .vault_stub_fetcher import VAULT_MODELS, VaultStubFetcher

FETCHERS = {"git": GitFetcher, "http_archive": HttpArchiveFetcher, "api": ApiFetcher, "drop_zone": DropZoneIntake,
            "vault": VaultStubFetcher}


def fetcher_for(spec, lake, registry, **kwargs) -> BaseFetcher:
    cls = FETCHERS[spec.access]
    if cls is not GitFetcher:
        kwargs.pop("runner", None)
    return cls(spec, lake, registry, **kwargs)


__all__ = ["ApiFetcher", "BaseFetcher", "DropZoneIntake", "Entitlement", "FETCHERS", "FetchError", "FetchOutcome",
           "GitFetcher", "HostRateLimiter", "HttpArchiveFetcher", "LiveLookupUnavailable", "LiveResult",
           "MissingCredentialError", "VAULT_MODELS", "VaultStubFetcher", "fetcher_for", "safe_extract", "sniff_mime"]

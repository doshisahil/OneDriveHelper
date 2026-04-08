"""Authentication helpers for Microsoft Graph."""

from __future__ import annotations

import os
from typing import Optional

from azure.core.exceptions import ClientAuthenticationError
from azure.identity import (
    AuthenticationRecord,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
)

from onedrive_helper.config import AUTH_RECORD_FILE, SCOPES, TOKEN_CACHE_NAME


def _load_auth_record() -> Optional[AuthenticationRecord]:
    if not os.path.exists(AUTH_RECORD_FILE):
        return None

    with open(AUTH_RECORD_FILE, encoding="utf-8") as file_handle:
        return AuthenticationRecord.deserialize(file_handle.read())


def _save_auth_record(record: AuthenticationRecord) -> None:
    with open(AUTH_RECORD_FILE, "w", encoding="utf-8") as file_handle:
        file_handle.write(record.serialize())


def get_credential(
    client_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> InteractiveBrowserCredential:
    """Create a reusable interactive browser credential."""
    resolved_client_id = client_id or os.getenv("CLIENT_ID")
    resolved_tenant_id = tenant_id or os.getenv("TENANT_ID", "consumers")
    if not resolved_client_id:
        raise ValueError("CLIENT_ID environment variable is required.")

    cache_options = TokenCachePersistenceOptions(
        name=TOKEN_CACHE_NAME,
        allow_unencrypted_storage=False,
    )
    auth_record = _load_auth_record()
    credential = InteractiveBrowserCredential(
        client_id=resolved_client_id,
        tenant_id=resolved_tenant_id,
        cache_persistence_options=cache_options,
        authentication_record=auth_record,
    )

    if auth_record is None:
        try:
            _save_auth_record(credential.authenticate(scopes=SCOPES))
        except (ClientAuthenticationError, OSError, ValueError):
            pass

    return credential

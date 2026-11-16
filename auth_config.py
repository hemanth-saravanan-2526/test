"""
OpenAI credential provisioning for the Inspection Agent Service.

Centralizes provisioning and validation for OpenAI credentials before any agent
session or API service startup, and fails loudly and early if credentials are
missing or malformed - never let a job silently run unauthenticated.

Supports two primary auth backends:
  - Direct OpenAI API key      (OPENAI_API_KEY, optional OPENAI_ORG_ID, OPENAI_PROJECT_ID, OPENAI_BASE_URL)
  - Azure OpenAI Service       (AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT)

Direct OpenAI API key is default.
"""

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

try:
    from dotenv import load_dotenv
    # Load .env from current directory or workspace root
    load_dotenv(override=False)
except ImportError:
    pass


class AuthBackend(str, Enum):
    API_KEY = "api_key"
    AZURE_OPENAI = "azure_openai"


class CredentialError(RuntimeError):
    """Raised when credentials are missing or malformed. Fail fast on this -
    never let a job start against an agent that can't authenticate."""


@dataclass
class ResolvedCredentials:
    backend: AuthBackend
    detail: str  # human-readable, safe to log (never contains the secret itself)
    api_key: str | None = None
    base_url: str | None = None
    org_id: str | None = None
    project_id: str | None = None


def _mask(secret: str) -> str:
    if not secret:
        return "****"
    if len(secret) <= 8:
        return "****"
    return f"{secret[:7]}...{secret[-4:]}"


def get_secret_from_manager(secret_name: str) -> str | None:
    """
    Extension point: wire this to your actual secrets manager (AWS Secrets
    Manager, GCP Secret Manager, HashiCorp Vault, Azure Key Vault, etc.)
    instead of relying on a plaintext env var in production. Called only
    as a fallback when the env var itself isn't set.

    Returns None if not implemented / not found, so callers fall through to
    the "missing" error path rather than crashing here.
    """
    return None


def resolve_credentials() -> ResolvedCredentials:
    """
    Call this once at service startup (see api_service.py's startup hook)
    and again defensively before each job if you rotate keys frequently.
    Raises CredentialError with a specific, actionable message on failure -
    never returns a partially-valid state.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(override=False)
    except ImportError:
        pass

    # Azure OpenAI backend
    azure_key = os.environ.get("AZURE_OPENAI_API_KEY")
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    if azure_key or azure_endpoint:
        if not azure_key or not azure_endpoint:
            raise CredentialError(
                "Azure OpenAI configured partially: both AZURE_OPENAI_API_KEY "
                "and AZURE_OPENAI_ENDPOINT must be provided."
            )
        return ResolvedCredentials(
            backend=AuthBackend.AZURE_OPENAI,
            detail=f"Azure OpenAI Endpoint ({azure_endpoint}, key: {_mask(azure_key)})",
            api_key=azure_key,
            base_url=azure_endpoint,
        )

    # Direct OpenAI API key path
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_AUTH_TOKEN")

    if not api_key:
        fetched = get_secret_from_manager("openai-api-key")
        if fetched:
            os.environ["OPENAI_API_KEY"] = fetched
            api_key = fetched

    if not api_key:
        raise CredentialError(
            "No OpenAI credentials found. Set OPENAI_API_KEY in the environment, "
            "wire get_secret_from_manager() to your secrets store, or set "
            "AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT for Azure OpenAI."
        )

    clean_key = api_key.strip().strip("'\"")

    if not clean_key.startswith("sk-"):
        raise CredentialError(
            f"OPENAI_API_KEY does not match the expected 'sk-...' format "
            f"(got {_mask(clean_key)}). Check for a copy-paste error or a stray "
            "quote/whitespace character in your env file."
        )

    base_url = os.environ.get("OPENAI_BASE_URL")
    org_id = os.environ.get("OPENAI_ORG_ID")
    project_id = os.environ.get("OPENAI_PROJECT_ID")

    details = [f"Direct OpenAI API key ({_mask(clean_key)})"]
    if base_url:
        details.append(f"base_url={base_url}")
    if org_id:
        details.append(f"org={org_id}")

    return ResolvedCredentials(
        backend=AuthBackend.API_KEY,
        detail=", ".join(details),
        api_key=clean_key,
        base_url=base_url,
        org_id=org_id,
        project_id=project_id,
    )


def require_credentials_at_startup() -> ResolvedCredentials:
    """Call this from the FastAPI startup event. Crashes the process on
    failure - deliberately, since a service that can't authenticate should
    not come up and start silently failing jobs one at a time."""
    creds = resolve_credentials()
    print(f"[inspection-service] Authenticated via {creds.detail}")
    return creds

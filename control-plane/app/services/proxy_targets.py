"""Helpers for validating and constructing internal proxy targets."""

from __future__ import annotations

from ipaddress import ip_address

from app.config import Settings
from app.schemas import RuntimeEndpointSummary, RuntimeRef

LOCALHOST_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


class RuntimeProxyValidationError(ValueError):
    """Raised when a stored runtime target is not allowed."""


def build_runtime_health_url(runtime_ref: RuntimeRef) -> str:
    """Build the runtime health URL from structured metadata."""

    return (
        f"{runtime_ref.transport.scheme}://"
        f"{runtime_ref.transport.host}:{runtime_ref.transport.port}"
        f"{runtime_ref.health.path}"
    )


def build_runtime_upstream_url(runtime_ref: RuntimeRef, path: str = "", query: str = "") -> str:
    """Build a proxy upstream URL from structured runtime metadata."""

    joined_path = join_url_path(runtime_ref.proxy.base_path, validate_runtime_path(path))
    query_suffix = f"?{query}" if query else ""
    return (
        f"{runtime_ref.transport.scheme}://"
        f"{runtime_ref.transport.host}:{runtime_ref.transport.port}"
        f"{joined_path}{query_suffix}"
    )


def join_url_path(base_path: str, path: str = "") -> str:
    """Join proxy-controlled base and request path segments."""

    segments = [segment.strip("/") for segment in (base_path, path) if segment and segment.strip("/")]
    if not segments:
        return "/"
    return "/" + "/".join(segments)


def validate_runtime_path(path: str) -> str:
    """Reject unsafe proxy path fragments from browser input."""

    if not path:
        return ""
    lowered = path.lower()
    if path.startswith("/") or path.startswith("\\"):
        raise RuntimeProxyValidationError("proxy path must be relative")
    if any(token in lowered for token in ("..", "%2f", "%5c", "http://", "https://", "//", "\\\\")):
        raise RuntimeProxyValidationError("proxy path is not allowed")
    return path


def validate_runtime_ref(settings: Settings, runtime_ref: RuntimeRef) -> None:
    """Ensure proxy targets stay constrained to expected internal runtime hosts."""

    scheme = runtime_ref.transport.scheme
    host = runtime_ref.transport.host
    normalized_host = host.strip()
    lowered_host = normalized_host.lower()

    if scheme not in settings.proxy_allowed_schemes:
        raise RuntimeProxyValidationError(f"runtime proxy scheme '{scheme}' is not allowed")
    if normalized_host != runtime_ref.service_name:
        raise RuntimeProxyValidationError("runtime proxy host must match service_name")
    if not settings.proxy_allow_localhost_hosts and lowered_host in LOCALHOST_HOSTS:
        raise RuntimeProxyValidationError("runtime proxy host cannot be localhost")
    if not settings.proxy_allow_ip_hosts:
        try:
            ip_address(normalized_host.strip("[]"))
        except ValueError:
            pass
        else:
            raise RuntimeProxyValidationError("runtime proxy host cannot be an IP literal")
    if settings.proxy_allowed_hosts and normalized_host not in settings.proxy_allowed_hosts:
        raise RuntimeProxyValidationError("runtime proxy host is not in the allowlist")
    if settings.proxy_allowed_host_suffixes and not any(
        normalized_host.endswith(suffix) for suffix in settings.proxy_allowed_host_suffixes
    ):
        raise RuntimeProxyValidationError("runtime proxy host suffix is not allowed")
    if runtime_ref.transport.port <= 0:
        raise RuntimeProxyValidationError("runtime proxy port must be positive")
    if not runtime_ref.health.path.startswith("/"):
        raise RuntimeProxyValidationError("runtime health path must be absolute")
    if runtime_ref.proxy.base_path and not runtime_ref.proxy.base_path.startswith("/"):
        raise RuntimeProxyValidationError("runtime proxy base_path must be absolute")


def runtime_endpoint_summary(runtime_ref: RuntimeRef) -> RuntimeEndpointSummary:
    """Expose a safe summary of the active runtime endpoint."""

    return RuntimeEndpointSummary(
        service_name=runtime_ref.service_name,
        host=runtime_ref.transport.host,
        port=runtime_ref.transport.port,
        proxy_base_path=runtime_ref.proxy.base_path,
        health_path=runtime_ref.health.path,
    )

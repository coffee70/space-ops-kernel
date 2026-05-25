from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas import RuntimeHealth, RuntimeProxy, RuntimeRef, RuntimeTransport
from app.services.proxy_targets import RuntimeProxyValidationError, validate_runtime_path, validate_runtime_ref


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings()


def _runtime_ref(*, service_name: str = "internal-runtime", host: str = "internal-runtime", scheme: str = "http") -> RuntimeRef:
    return RuntimeRef(
        service_name=service_name,
        transport=RuntimeTransport(scheme=scheme, host=host, port=8080),
        health=RuntimeHealth(path="/health"),
        proxy=RuntimeProxy(base_path=""),
    )


def test_runtime_proxy_validation_rejects_ip_hosts() -> None:
    settings = Settings(database_url="postgresql://telemetry:telemetry@localhost:5432/control_plane_test")

    with pytest.raises(RuntimeProxyValidationError, match="IP literal"):
        validate_runtime_ref(settings, _runtime_ref(service_name="10.0.0.8", host="10.0.0.8"))


def test_runtime_proxy_validation_rejects_localhost() -> None:
    settings = Settings(database_url="postgresql://telemetry:telemetry@localhost:5432/control_plane_test")

    with pytest.raises(RuntimeProxyValidationError, match="localhost"):
        validate_runtime_ref(settings, _runtime_ref(service_name="localhost", host="localhost"))


def test_runtime_proxy_validation_rejects_scheme_mismatch() -> None:
    settings = Settings(database_url="postgresql://telemetry:telemetry@localhost:5432/control_plane_test")
    runtime_ref = RuntimeRef.model_construct(
        service_name="internal-runtime",
        transport=RuntimeTransport.model_construct(scheme="https", host="internal-runtime", port=8080),
        health=RuntimeHealth(path="/health"),
        proxy=RuntimeProxy(base_path=""),
    )

    with pytest.raises(RuntimeProxyValidationError, match="scheme"):
        validate_runtime_ref(settings, runtime_ref)


def test_runtime_proxy_validation_rejects_host_service_name_mismatch() -> None:
    settings = Settings(database_url="postgresql://telemetry:telemetry@localhost:5432/control_plane_test")

    with pytest.raises(RuntimeProxyValidationError, match="service_name"):
        validate_runtime_ref(
            settings,
            _runtime_ref(service_name="internal-runtime", host="unexpected-runtime"),
        )


def test_validate_runtime_path_allows_next_static_css_filename_with_double_dots() -> None:
    path = "_next/static/chunks/15_0uk_ih._1..css"

    assert validate_runtime_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "../server.js",
        "_next/static/../server.js",
        "_next/static/%2e%2e/server.js",
    ],
)
def test_validate_runtime_path_rejects_traversal_segments(path: str) -> None:
    with pytest.raises(RuntimeProxyValidationError, match="proxy path is not allowed"):
        validate_runtime_path(path)

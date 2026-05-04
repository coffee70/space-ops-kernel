"""Template catalog and scaffolding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.config import Settings
from app.git.repository import ManagedGitRepository
from app.schemas import ScaffoldRequest, UnitManifest


class TemplateService:
    """Load templates and scaffold managed runtimes."""

    def __init__(self, settings: Settings, repository: ManagedGitRepository):
        self.settings = settings
        self.repository = repository
        self.templates_root = Path(__file__).resolve().parents[1] / "templates"

    def list_templates(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for template_dir in sorted(path for path in self.templates_root.iterdir() if path.is_dir()):
            items.append(self.get_template(template_dir.name))
        return items

    def get_template(self, template_id: str) -> dict[str, Any]:
        template_dir = self.templates_root / template_id
        if not template_dir.exists():
            raise FileNotFoundError(template_id)
        with (template_dir / "template.yaml").open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    def scaffold(self, template_id: str, request: ScaffoldRequest) -> dict[str, Any]:
        template = self.get_template(template_id)
        branch = request.branch
        worktree = self.repository.ensure_branch_worktree(branch)
        manifest_path = Path("manifests/units") / f"{request.unit_id}.yaml"
        if (worktree / manifest_path).exists():
            raise ValueError(f"unit_id {request.unit_id} already exists")

        allowed_owners = template["allowed_package_owners"]
        package_owner = request.package_owner or allowed_owners[0]
        if package_owner not in allowed_owners:
            raise ValueError(f"package_owner must be one of {allowed_owners}")

        source_path = Path(request.source_path or self._default_source_path(template_id, request.unit_id, package_owner))
        self.repository.normalize_code_path(source_path.as_posix())
        if (worktree / source_path).exists():
            raise ValueError(f"source_path {source_path.as_posix()} already exists")

        route_slug = request.discovery.get("application_id") or request.discovery.get("route_slug") or request.unit_id
        replacements = {
            "unit_id": request.unit_id,
            "display_name": request.display_name,
            "package_owner": package_owner,
            "source_path": source_path.as_posix(),
            "application_id": route_slug,
            "route_slug": route_slug,
            "category": request.discovery.get("category") or template.get("default_category"),
            "api_base_path": request.discovery.get("api_base_path") or f"/api/{request.unit_id}",
            "description": request.discovery.get("description") or f"{request.display_name} application",
            "default_build_command": template["default_build_command"],
            "default_run_command": template["default_run_command"],
            "health_path": template["health"]["path"],
        }

        changed_files: list[str] = []
        files_root = self.templates_root / template_id / "files"
        for source_file in sorted(files_root.rglob("*")):
            if source_file.is_dir() or "__pycache__" in source_file.parts or source_file.suffix == ".pyc":
                continue
            relative = source_file.relative_to(files_root)
            rendered_relative = self._render_template_string(relative.as_posix(), replacements)
            target = worktree / source_path / rendered_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                self._render_template_string(source_file.read_text(encoding="utf-8"), replacements),
                encoding="utf-8",
            )
            changed_files.append(target.relative_to(worktree).as_posix())

        manifest = UnitManifest(
            unit_id=request.unit_id,
            display_name=request.display_name,
            package_owner=package_owner,
            runtime_kind=template["runtime_kind"],
            runtime_template=template_id,
            source_path=source_path.as_posix(),
            build={"command": template["default_build_command"]},
            run={"command": template["default_run_command"]},
            health=template["health"],
            discovery=self._default_discovery(template_id, replacements, request.discovery),
            application=self._default_application(template_id, request.display_name, package_owner, replacements, request.discovery),
        )
        manifest_target = worktree / manifest_path
        manifest_target.parent.mkdir(parents=True, exist_ok=True)
        manifest_target.write_text(yaml.safe_dump(manifest.model_dump(), sort_keys=False), encoding="utf-8")
        changed_files.append(manifest_path.as_posix())

        return {
            "branch": branch,
            "path": manifest_path.as_posix(),
            "commit_sha": self.repository.get_head_commit(branch),
            "changed_files": changed_files,
            "manifest": manifest.model_dump(),
        }

    def _default_source_path(self, template_id: str, unit_id: str, package_owner: str) -> str:
        if template_id == "python-service":
            return f"project/space-ops-platform/backend/services/{unit_id}"
        if template_id == "node-service":
            if package_owner == "space-ops-platform":
                return f"project/space-ops-platform/backend/services/{unit_id}"
            return f"project/space-ops-apps/services/{unit_id}"
        if template_id in {"frontend-native-application", "frontend-embedded-application"}:
            return f"project/space-ops-apps/applications/{unit_id}"
        if template_id == "frontend-shell":
            return "project/space-ops-apps/mission-control-ui"
        raise ValueError(f"unsupported template {template_id}")

    @staticmethod
    def _render_template_string(value: str, replacements: dict[str, Any]) -> str:
        rendered = value
        for key, replacement in replacements.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(replacement))
            rendered = rendered.replace(f"__{key}__", str(replacement))
        return rendered

    @staticmethod
    def _default_discovery(template_id: str, replacements: dict[str, Any], discovery: dict[str, Any]) -> dict[str, Any]:
        if template_id in {"frontend-native-application", "frontend-embedded-application", "frontend-shell"}:
            return {}
        return {
            "service_slug": discovery.get("service_slug") or replacements["unit_id"],
            "category": replacements["category"],
            "api_base_path": replacements["api_base_path"],
            "capability_tags": discovery.get("capability_tags") or [],
            "capabilities": discovery.get("capabilities") or discovery.get("capability_tags") or [],
            "health_endpoint": discovery.get("health_endpoint") or replacements.get("health_path", "/health"),
        }

    @staticmethod
    def _default_application(
        template_id: str,
        display_name: str,
        package_owner: str,
        replacements: dict[str, Any],
        discovery: dict[str, Any],
    ) -> dict[str, Any] | None:
        if template_id == "frontend-native-application":
            return {
                "application_id": replacements["application_id"],
                "title": display_name,
                "description": discovery.get("description") or f"{display_name} native application",
                "icon_key": discovery.get("icon_key") or "app-window",
                "icon_color": discovery.get("icon_color") or "#38bdf8",
                "icon_background": discovery.get("icon_background") or "rgba(56, 189, 248, 0.16)",
                "application_type": "native",
                "route_path": f"/apps/{replacements['application_id']}",
                "loader_key": discovery.get("loader_key") or replacements["application_id"],
                "version": discovery.get("version") or "0.1.0",
                "enabled": True,
                "sort_order": discovery.get("sort_order") or 100,
                "owner": discovery.get("owner") or package_owner,
                "capabilities": discovery.get("capabilities") or [],
            }
        if template_id == "frontend-embedded-application":
            return {
                "application_id": replacements["application_id"],
                "title": display_name,
                "description": discovery.get("description") or f"{display_name} embedded application",
                "icon_key": discovery.get("icon_key") or "app-window",
                "icon_color": discovery.get("icon_color") or "#f59e0b",
                "icon_background": discovery.get("icon_background") or "rgba(245, 158, 11, 0.16)",
                "application_type": "embedded",
                "route_path": f"/apps/{replacements['application_id']}",
                "embedded_url": discovery.get("embedded_url"),
                "proxy_base_path": discovery.get("proxy_base_path") or f"/runtime-applications/{replacements['application_id']}",
                "version": discovery.get("version") or "0.1.0",
                "enabled": True,
                "iframe_sandbox": discovery.get("iframe_sandbox") or "allow-scripts allow-same-origin allow-forms",
                "iframe_allow": discovery.get("iframe_allow") or "",
                "sort_order": discovery.get("sort_order") or 100,
                "owner": discovery.get("owner") or package_owner,
                "capabilities": discovery.get("capabilities") or [],
            }
        return None

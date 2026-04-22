"""Template catalog and scaffolding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.actors import ActorContext
from app.config import Settings
from app.git.repository import ManagedGitRepository
from app.schemas import ScaffoldRequest, UnitManifest


class TemplateService:
    """Load templates and scaffold units."""

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

    def scaffold(self, template_id: str, request: ScaffoldRequest, actor: ActorContext) -> dict[str, Any]:
        template = self.get_template(template_id)
        branch = request.branch
        worktree = self.repository.ensure_branch_worktree(branch)
        manifest_path = Path("manifests/units") / f"{request.unit_id}.yaml"
        if (worktree / manifest_path).exists():
            raise ValueError(f"unit_id {request.unit_id} already exists")

        source_path = Path(request.source_path or self._default_source_path(template_id, request.unit_id))
        self.repository.normalize_code_path(source_path.as_posix())
        if (worktree / source_path).exists():
            raise ValueError(f"source_path {source_path.as_posix()} already exists")

        allowed_owners = template["allowed_package_owners"]
        package_owner = request.package_owner or allowed_owners[0]
        if package_owner not in allowed_owners:
            raise ValueError(f"package_owner must be one of {allowed_owners}")

        replacements = {
            "unit_id": request.unit_id,
            "display_name": request.display_name,
            "package_owner": package_owner,
            "source_path": source_path.as_posix(),
            "route_slug": request.discovery.get("route_slug") or request.unit_id,
            "category": request.discovery.get("category") or template.get("default_category"),
            "api_base_path": request.discovery.get("api_base_path") or f"/api/{request.unit_id}",
            "description": request.discovery.get("description") or f"{request.display_name} module",
            "default_build_command": template["default_build_command"],
            "default_run_command": template["default_run_command"],
            "health_path": template["health"]["path"],
        }

        changed_files: list[str] = []
        files_root = self.templates_root / template_id / "files"
        for source_file in sorted(files_root.rglob("*")):
            if source_file.is_dir():
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
            unit_kind=template["unit_kind"],
            runtime_template=template_id,
            source_path=source_path.as_posix(),
            build={"command": template["default_build_command"]},
            run={"command": template["default_run_command"]},
            health=template["health"],
            discovery=self._default_discovery(template_id, replacements, request.discovery),
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
            "actor": {"actor_id": actor.actor_id, "display_name": actor.display_name},
            "manifest": manifest.model_dump(),
        }

    def _default_source_path(self, template_id: str, unit_id: str) -> str:
        if template_id == "python-service":
            return f"project/space-ops-platform/backend/services/{unit_id}"
        if template_id == "node-service":
            return f"project/space-ops-apps/services/{unit_id}"
        if template_id == "frontend-module":
            return f"project/space-ops-apps/modules/{unit_id}"
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
        if template_id == "frontend-module":
            return {
                "route_slug": replacements["route_slug"],
                "display_name": replacements["display_name"],
                "description": discovery.get("description") or f"{replacements['display_name']} workspace module",
                "icon_key": discovery.get("icon_key") or "app-window",
                "open_path": f"/modules/{replacements['route_slug']}",
            }
        return {
            "category": replacements["category"],
            "api_base_path": replacements["api_base_path"],
            "capability_tags": discovery.get("capability_tags") or [],
            "health_endpoint": discovery.get("health_endpoint") or replacements.get("health_path", "/health"),
        }

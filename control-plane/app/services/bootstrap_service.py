"""Managed fork and runtime bootstrap."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx
import yaml

from app.config import Settings
from app.db import get_session_factory
from app.deployments.service import DeploymentService
from app.git.repository import ManagedGitRepository
from app.registry.service import RegistryService
from app.schemas import DeploymentSubmissionRequest, RuntimeRef, UnitManifest
from app.services.proxy_targets import build_runtime_health_url
from app.services.shell import run_command

logger = logging.getLogger(__name__)

IGNORE_NAMES = shutil.ignore_patterns(
    ".git",
    ".venv",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    ".next",
    "dist",
    "build",
    "tmp",
)

BOOTSTRAP_UNITS = (
    "vehicle-config-service",
    "source-registry-service",
    "telemetry-ingest-service",
    "satnogs-adapter-service",
    "simulator-service",
    "simulator-2-service",
    "position-orbit-service",
    "simulator-control-service",
    "telemetry-query-service",
    "telemetry-intelligence-service",
    "ops-events-service",
    "document-knowledge-service",
    "document-ingestion-worker",
    "tool-registry-service",
    "tool-execution-service",
    "code-intelligence-service",
    "code-indexer-worker",
    "context-retrieval-service",
    "model-registry-service",
    "agent-runtime-service",
    "platform-api-gateway",
    "derived-telemetry-service",
)

REGISTRY_SEED_UNITS = (
    *BOOTSTRAP_UNITS,
    "mission-control-frontend-shell",
)


@dataclass
class RuntimeUnitBootstrapResult:
    """Result for a single runtime bootstrap unit."""

    unit_id: str
    status: str
    deployment_id: str | None = None
    failure_reason: str | None = None


@dataclass
class RuntimeBootstrapResult:
    """Aggregate runtime bootstrap result."""

    units: list[RuntimeUnitBootstrapResult] = field(default_factory=list)

    @property
    def failed_units(self) -> list[RuntimeUnitBootstrapResult]:
        return [unit for unit in self.units if unit.status == "failed"]

    @property
    def blocked_units(self) -> list[RuntimeUnitBootstrapResult]:
        return [unit for unit in self.units if unit.status == "blocked"]

    @property
    def healthy_units(self) -> list[RuntimeUnitBootstrapResult]:
        return [unit for unit in self.units if unit.status in {"healthy", "current"}]

    def add(self, unit: RuntimeUnitBootstrapResult) -> None:
        self.units.append(unit)


@dataclass(frozen=True)
class DependencyCycle:
    units: tuple[str, ...]
    path: tuple[str, ...]


@dataclass(frozen=True)
class BlockedDependency:
    unit_id: str
    reason: str
    message: str
    blocking_units: tuple[str, ...] = ()


@dataclass
class BootstrapDependencyPlan:
    dependencies: dict[str, tuple[str, ...]]
    dependents: dict[str, tuple[str, ...]]
    cycles: list[DependencyCycle] = field(default_factory=list)
    invalid_dependencies: list[dict[str, str]] = field(default_factory=list)
    blocked: dict[str, BlockedDependency] = field(default_factory=dict)

    def dependency_issues(self) -> dict[str, list[dict]]:
        return {
            "cycles": [{"units": list(cycle.units), "path": list(cycle.path)} for cycle in self.cycles],
            "blocked_units": [
                {
                    "unit_id": blocked.unit_id,
                    "reason": blocked.reason,
                    "blocking_units": list(blocked.blocking_units),
                }
                for blocked in self.blocked.values()
            ],
            "invalid_dependencies": list(self.invalid_dependencies),
        }


class RuntimeBootstrapStatusTracker(Protocol):
    """Subset of status-service methods used by the bootstrapper."""

    def mark_unit_deploying(self, run_id: int, unit_id: str) -> None: ...

    def mark_unit_current(self, run_id: int, unit_id: str, deployment_id: str | None = None) -> None: ...

    def mark_unit_skipped(self, run_id: int, unit_id: str, reason: str | None = None) -> None: ...

    def mark_unit_healthy(self, run_id: int, unit_id: str, deployment_id: str | None = None) -> None: ...

    def mark_unit_failed(self, run_id: int, unit_id: str, reason: str, deployment_id: str | None = None) -> None: ...

    def mark_unit_blocked(self, run_id: int, unit_id: str, reason: str, deployment_id: str | None = None) -> None: ...

    def set_dependency_issues(self, run_id: int, dependency_issues: dict) -> None: ...


class ManagedForkBootstrapper:
    """Create the managed fork and preserve it as durable editable state."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def ensure_bootstrapped(self) -> None:
        """Ensure the managed fork exists without overwriting existing managed code."""

        self.settings.ensure_runtime_dirs()
        repo_missing = not self.settings.bare_repo_dir.exists() or not any(self.settings.bare_repo_dir.iterdir())
        if repo_missing:
            self._initialize_managed_repo()
        self._ensure_main_worktree()
        self._sync_bootstrap_manifests()

    def _initialize_managed_repo(self) -> None:
        self.settings.bare_repo_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=self.settings.resolved_runtime_root) as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            self._materialize_import_tree(temp_dir)
            run_command(["git", "init", "-b", "main"], cwd=temp_dir)
            run_command(["git", "config", "user.name", "Space Ops Control Plane"], cwd=temp_dir)
            run_command(["git", "config", "user.email", "control-plane@space-ops.local"], cwd=temp_dir)
            run_command(["git", "add", "."], cwd=temp_dir)
            run_command(["git", "commit", "-m", "Initial managed fork import"], cwd=temp_dir)

            if not any(self.settings.bare_repo_dir.iterdir()):
                run_command(["git", "init", "--bare", str(self.settings.bare_repo_dir)])

            run_command(["git", "remote", "add", "origin", str(self.settings.bare_repo_dir)], cwd=temp_dir)
            run_command(["git", "push", "--force", "origin", "main"], cwd=temp_dir)

    def _ensure_main_worktree(self) -> None:
        if self.settings.main_worktree_dir.exists():
            return
        run_command(
            [
                "git",
                f"--git-dir={self.settings.bare_repo_dir}",
                "worktree",
                "add",
                str(self.settings.main_worktree_dir),
                "main",
            ]
        )

    def _materialize_import_tree(self, root: Path) -> None:
        project_root = root / "project"
        manifests_root = root / "manifests" / "units"
        project_root.mkdir(parents=True, exist_ok=True)
        manifests_root.mkdir(parents=True, exist_ok=True)

        shutil.copytree(
            self.settings.resolved_platform_source_root,
            project_root / "space-ops-platform",
            ignore=IGNORE_NAMES,
            dirs_exist_ok=True,
        )
        shutil.copytree(
            self.settings.resolved_apps_source_root,
            project_root / "space-ops-apps",
            ignore=IGNORE_NAMES,
            dirs_exist_ok=True,
        )

        seed_root = self._seed_manifests_root()
        for manifest_path in seed_root.glob("*.yaml"):
            shutil.copy2(manifest_path, manifests_root / manifest_path.name)

    def _seed_manifests_root(self) -> Path:
        return Path(__file__).resolve().parents[1] / "bootstrap" / "manifests"

    def _sync_bootstrap_manifests(self) -> None:
        worktree = self.settings.main_worktree_dir

        previous_state = self._load_seed_state()
        next_state = dict(previous_state)
        updated = False
        updated_paths: list[str] = []

        for relative_path, seed_path in self._seeded_bootstrap_files():
            target_path = worktree / relative_path
            seed_content = seed_path.read_text(encoding="utf-8")
            seed_hash = self._content_hash(seed_content)
            previous_hash = previous_state.get(relative_path.as_posix())
            current_hash = self._content_hash(target_path.read_text(encoding="utf-8")) if target_path.exists() else None

            should_replace = False
            if not target_path.exists():
                should_replace = True
            elif previous_hash is not None:
                should_replace = current_hash == previous_hash

            if should_replace:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                if not target_path.exists() or target_path.read_text(encoding="utf-8") != seed_content:
                    target_path.write_text(seed_content, encoding="utf-8")
                    updated = True
                    updated_paths.append(relative_path.as_posix())
                next_state[relative_path.as_posix()] = seed_hash
            elif previous_hash is None and current_hash == seed_hash:
                next_state[relative_path.as_posix()] = seed_hash

        self._write_seed_state(next_state)
        if updated:
            self._commit_synced_manifests(updated_paths)

    def _seeded_bootstrap_files(self) -> list[tuple[Path, Path]]:
        seeded_files: list[tuple[Path, Path]] = []

        for manifest_path in sorted(self._seed_manifests_root().glob("*.yaml")):
            seeded_files.append((Path("manifests") / "units" / manifest_path.name, manifest_path))

        return seeded_files

    def _commit_synced_manifests(self, updated_paths: list[str]) -> None:
        if not updated_paths:
            return
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "Space Ops Control Plane",
                "GIT_AUTHOR_EMAIL": "control-plane@space-ops.local",
                "GIT_COMMITTER_NAME": "Space Ops Control Plane",
                "GIT_COMMITTER_EMAIL": "control-plane@space-ops.local",
            }
        )
        run_command(["git", "add", *updated_paths], cwd=self.settings.main_worktree_dir)
        run_command(
            ["git", "commit", "-m", "Sync bootstrap manifests from control plane", "--", *updated_paths],
            cwd=self.settings.main_worktree_dir,
            env=env,
        )

    def _load_seed_state(self) -> dict[str, str]:
        path = self.settings.bootstrap_seed_state_path
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_seed_state(self, state: dict[str, str]) -> None:
        self.settings.bootstrap_seed_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.bootstrap_seed_state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


class RuntimeBootstrapper:
    """Ensure required managed units are deployed for the local stack."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def seed_bootstrap_unit_registry(self) -> None:
        session_factory = get_session_factory()
        with session_factory() as session:
            registry = RegistryService(session)
            for manifest_path in sorted((self.settings.main_worktree_dir / "manifests" / "units").glob("*.yaml")):
                if manifest_path.stem not in REGISTRY_SEED_UNITS:
                    continue
                manifest = UnitManifest.model_validate(yaml.safe_load(manifest_path.read_text(encoding="utf-8")))
                registry.seed_manifest_unit(manifest)
            session.commit()

    def ensure_bootstrapped(
        self,
        *,
        fail_fast: bool = False,
        status_tracker: RuntimeBootstrapStatusTracker | None = None,
        run_id: int | None = None,
    ) -> RuntimeBootstrapResult:
        repository = ManagedGitRepository(self.settings)
        commit_sha = repository.get_head_commit("main")
        session_factory = get_session_factory()
        bootstrap_result = RuntimeBootstrapResult()
        manifests = self._load_bootstrap_manifests()
        plan = self._build_dependency_plan(manifests)
        unit_statuses: dict[str, str] = {}

        for unit_id in BOOTSTRAP_UNITS:
            blocked = plan.blocked.get(unit_id)
            if blocked is None:
                continue
            unit_result = RuntimeUnitBootstrapResult(unit_id, "blocked", failure_reason=blocked.message)
            bootstrap_result.add(unit_result)
            unit_statuses[unit_id] = "blocked"
            self._record_status(status_tracker, run_id, unit_result)
            logger.error("runtime bootstrap unit blocked: %s reason=%s", unit_id, blocked.message)

        self._record_dependency_issues(status_tracker, run_id, plan)

        while len(unit_statuses) < len(BOOTSTRAP_UNITS):
            ready_units = self._ready_units(plan.dependencies, unit_statuses)
            if not ready_units:
                unresolved = [unit_id for unit_id in BOOTSTRAP_UNITS if unit_id not in unit_statuses]
                for unit_id in unresolved:
                    message = "Blocked because required dependencies could not become available."
                    unit_result = RuntimeUnitBootstrapResult(unit_id, "blocked", failure_reason=message)
                    bootstrap_result.add(unit_result)
                    unit_statuses[unit_id] = "blocked"
                    plan.blocked[unit_id] = BlockedDependency(unit_id, "dependency_unavailable", message)
                    self._record_status(status_tracker, run_id, unit_result)
                self._record_dependency_issues(status_tracker, run_id, plan)
                break

            wave_results = self._run_parallel_wave(
                ready_units,
                repository=repository,
                commit_sha=commit_sha,
                session_factory=session_factory,
                status_tracker=status_tracker,
                run_id=run_id,
                fail_fast=fail_fast,
            )
            for unit_result in wave_results:
                bootstrap_result.add(unit_result)
                unit_statuses[unit_result.unit_id] = unit_result.status
                self._record_status(status_tracker, run_id, unit_result)

            new_blocked = self._blocked_by_unsatisfied_dependencies(plan, unit_statuses)
            for blocked in new_blocked:
                unit_result = RuntimeUnitBootstrapResult(blocked.unit_id, "blocked", failure_reason=blocked.message)
                bootstrap_result.add(unit_result)
                unit_statuses[blocked.unit_id] = "blocked"
                plan.blocked[blocked.unit_id] = blocked
                self._record_status(status_tracker, run_id, unit_result)
                logger.error("runtime bootstrap unit blocked: %s reason=%s", blocked.unit_id, blocked.message)
            if new_blocked:
                self._record_dependency_issues(status_tracker, run_id, plan)

        summary = {
            "healthy": len([unit for unit in bootstrap_result.units if unit.status == "healthy"]),
            "current": len([unit for unit in bootstrap_result.units if unit.status == "current"]),
            "skipped": len([unit for unit in bootstrap_result.units if unit.status == "skipped"]),
            "failed": len(bootstrap_result.failed_units),
            "blocked": len(bootstrap_result.blocked_units),
        }
        logger.info(
            "runtime bootstrap completed: healthy=%s current=%s skipped=%s failed=%s blocked=%s",
            summary["healthy"],
            summary["current"],
            summary["skipped"],
            summary["failed"],
            summary["blocked"],
        )
        return bootstrap_result

    def _load_bootstrap_manifests(self) -> dict[str, UnitManifest]:
        manifests: dict[str, UnitManifest] = {}
        manifest_root = self.settings.main_worktree_dir / "manifests" / "units"
        for unit_id in BOOTSTRAP_UNITS:
            manifest_path = manifest_root / f"{unit_id}.yaml"
            if not manifest_path.is_file():
                raise FileNotFoundError(f"bootstrap manifest not found for {unit_id}")
            manifests[unit_id] = UnitManifest.model_validate(yaml.safe_load(manifest_path.read_text(encoding="utf-8")))
        return manifests

    def _build_dependency_plan(self, manifests: dict[str, UnitManifest]) -> BootstrapDependencyPlan:
        known_units = set(BOOTSTRAP_UNITS)
        dependencies: dict[str, tuple[str, ...]] = {}
        dependents: dict[str, list[str]] = {unit_id: [] for unit_id in BOOTSTRAP_UNITS}
        plan = BootstrapDependencyPlan(dependencies={}, dependents={})

        for unit_id in BOOTSTRAP_UNITS:
            manifest = manifests[unit_id]
            seen: set[str] = set()
            normalized: list[str] = []
            for dependency in manifest.dependencies:
                if dependency in seen:
                    continue
                seen.add(dependency)
                normalized.append(dependency)
                if dependency not in known_units:
                    message = f"Blocked because manifest dependency '{dependency}' is not a known bootstrap unit."
                    plan.invalid_dependencies.append({"unit_id": unit_id, "dependency": dependency})
                    plan.blocked[unit_id] = BlockedDependency(unit_id, "unknown_dependency", message, (dependency,))
            dependencies[unit_id] = tuple(normalized)
            for dependency in normalized:
                if dependency in dependents:
                    dependents[dependency].append(unit_id)

        plan.dependencies = dependencies
        plan.dependents = {unit_id: tuple(children) for unit_id, children in dependents.items()}

        cycles = self._detect_cycles(dependencies)
        plan.cycles = cycles
        for cycle in cycles:
            path = " -> ".join(cycle.path)
            for unit_id in cycle.units:
                plan.blocked[unit_id] = BlockedDependency(
                    unit_id,
                    "dependency_cycle",
                    f"Blocked by dependency cycle: {path}.",
                    cycle.units,
                )
            for downstream in self._collect_downstream(set(cycle.units), plan.dependents):
                if downstream in cycle.units or downstream in plan.blocked:
                    continue
                plan.blocked[downstream] = BlockedDependency(
                    downstream,
                    "dependency_blocked",
                    f"Blocked because dependency chain includes a cyclic unit: {cycle.units[0]}.",
                    (cycle.units[0],),
                )

        for blocked in list(plan.blocked.values()):
            if blocked.reason == "unknown_dependency":
                for downstream in self._collect_downstream({blocked.unit_id}, plan.dependents):
                    if downstream in plan.blocked:
                        continue
                    plan.blocked[downstream] = BlockedDependency(
                        downstream,
                        "dependency_blocked",
                        f"Blocked because dependency {blocked.unit_id} could not become available.",
                        (blocked.unit_id,),
                    )
        return plan

    def _detect_cycles(self, dependencies: dict[str, tuple[str, ...]]) -> list[DependencyCycle]:
        cycles: list[DependencyCycle] = []
        cycle_keys: set[frozenset[str]] = set()
        visiting: list[str] = []
        visited: set[str] = set()

        def visit(unit_id: str) -> None:
            if unit_id in visiting:
                start = visiting.index(unit_id)
                path = tuple(visiting[start:] + [unit_id])
                units = tuple(dict.fromkeys(path[:-1]))
                key = frozenset(units)
                if key not in cycle_keys:
                    cycle_keys.add(key)
                    cycles.append(DependencyCycle(units=units, path=path))
                return
            if unit_id in visited:
                return
            visiting.append(unit_id)
            for dependency in dependencies.get(unit_id, ()):
                if dependency in dependencies:
                    visit(dependency)
            visiting.pop()
            visited.add(unit_id)

        for unit_id in BOOTSTRAP_UNITS:
            visit(unit_id)
        return cycles

    def _collect_downstream(self, unit_ids: set[str], dependents: dict[str, tuple[str, ...]]) -> set[str]:
        blocked: set[str] = set()
        stack = list(unit_ids)
        while stack:
            current = stack.pop()
            for child in dependents.get(current, ()):
                if child in blocked:
                    continue
                blocked.add(child)
                stack.append(child)
        return blocked

    def _ready_units(self, dependencies: dict[str, tuple[str, ...]], unit_statuses: dict[str, str]) -> list[str]:
        satisfying = {"healthy", "current"}
        ready: list[str] = []
        for unit_id in BOOTSTRAP_UNITS:
            if unit_id in unit_statuses:
                continue
            if all(unit_statuses.get(dependency) in satisfying for dependency in dependencies.get(unit_id, ())):
                ready.append(unit_id)
        return ready

    def _run_parallel_wave(
        self,
        unit_ids: list[str],
        *,
        repository: ManagedGitRepository,
        commit_sha: str,
        session_factory,
        status_tracker: RuntimeBootstrapStatusTracker | None,
        run_id: int | None,
        fail_fast: bool,
    ) -> list[RuntimeUnitBootstrapResult]:
        for unit_id in unit_ids:
            if status_tracker is not None and run_id is not None:
                status_tracker.mark_unit_deploying(run_id, unit_id)

        max_workers = max(1, min(self.settings.runtime_bootstrap_max_parallel_deployments, len(unit_ids)))
        results: dict[str, RuntimeUnitBootstrapResult] = {}
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="runtime-bootstrap") as executor:
            futures = {
                executor.submit(self._bootstrap_unit, unit_id, repository, commit_sha, session_factory, fail_fast): unit_id
                for unit_id in unit_ids
            }
            for future in as_completed(futures):
                unit_id = futures[future]
                results[unit_id] = future.result()
        return [results[unit_id] for unit_id in unit_ids]

    def _bootstrap_unit(
        self,
        unit_id: str,
        repository: ManagedGitRepository,
        commit_sha: str,
        session_factory,
        fail_fast: bool,
    ) -> RuntimeUnitBootstrapResult:
        deployment_id: str | None = None
        try:
            with session_factory() as session:
                deployment_service = DeploymentService(self.settings, repository, session)
                registry = RegistryService(session)
                current_deployment_id = self._current_deployment_id(registry, deployment_service, unit_id, commit_sha)
                if current_deployment_id is not None:
                    session.commit()
                    logger.info("runtime bootstrap unit current: %s", unit_id)
                    return RuntimeUnitBootstrapResult(unit_id, "current", current_deployment_id)

                if not self._bootstrap_source_exists(deployment_service, unit_id, commit_sha):
                    session.commit()
                    logger.info("runtime bootstrap unit skipped: %s", unit_id)
                    return RuntimeUnitBootstrapResult(unit_id, "skipped", failure_reason="bootstrap source missing")

                result = deployment_service.submit(
                    DeploymentSubmissionRequest(unit_id=unit_id, branch="main", commit_sha=commit_sha),
                    delete_eligible=False,
                )
                deployment_id = result.deployment_id
                session.commit()
                if result.status == "healthy":
                    logger.info("runtime bootstrap unit healthy: %s deployment=%s", unit_id, result.deployment_id)
                    return RuntimeUnitBootstrapResult(unit_id, "healthy", result.deployment_id)
                reason = result.failure_reason or result.status
                logger.error("runtime bootstrap unit failed: %s reason=%s", unit_id, reason)
                return RuntimeUnitBootstrapResult(unit_id, "failed", result.deployment_id, reason)
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            logger.exception("runtime bootstrap unit failed: %s reason=%s", unit_id, reason)
            if fail_fast:
                raise
            return RuntimeUnitBootstrapResult(unit_id, "failed", deployment_id, reason)

    def _blocked_by_unsatisfied_dependencies(
        self,
        plan: BootstrapDependencyPlan,
        unit_statuses: dict[str, str],
    ) -> list[BlockedDependency]:
        satisfying = {"healthy", "current"}
        unsatisfying = {
            unit_id: status
            for unit_id, status in unit_statuses.items()
            if status not in satisfying and status in {"failed", "blocked", "skipped"}
        }
        blocked: list[BlockedDependency] = []
        for unit_id in BOOTSTRAP_UNITS:
            if unit_id in unit_statuses or unit_id in plan.blocked:
                continue
            for dependency in plan.dependencies.get(unit_id, ()):
                status = unsatisfying.get(dependency)
                if status is None:
                    continue
                reason = "dependency_blocked" if status == "blocked" else "dependency_failed"
                state_text = "failed during bootstrap" if status == "failed" else "could not become available"
                message = f"Blocked because required dependency {dependency} {state_text}."
                blocked.append(BlockedDependency(unit_id, reason, message, (dependency,)))
                break
        return blocked

    def _record_dependency_issues(
        self,
        status_tracker: RuntimeBootstrapStatusTracker | None,
        run_id: int | None,
        plan: BootstrapDependencyPlan,
    ) -> None:
        if status_tracker is not None and run_id is not None:
            status_tracker.set_dependency_issues(run_id, plan.dependency_issues())

    def _bootstrap_source_exists(self, deployment_service: DeploymentService, unit_id: str, commit_sha: str) -> bool:
        manifest = deployment_service._load_manifest(commit_sha, unit_id)
        return (self.settings.main_worktree_dir / manifest.source_path).exists()

    def _current_deployment_id(
        self,
        registry: RegistryService,
        deployment_service: DeploymentService,
        unit_id: str,
        commit_sha: str,
    ) -> str | None:
        unit = registry.get_unit(unit_id)
        if unit is None or not unit.active_deployment_id:
            return None

        deployment = registry.get_deployment(unit.active_deployment_id)
        if deployment is None or deployment.status != "healthy" or deployment.commit_sha != commit_sha:
            return None
        if self.settings.runtime_strategy != "docker":
            return deployment.deployment_id

        if not deployment.runtime_ref:
            return None
        try:
            runtime_ref = RuntimeRef.model_validate(deployment.runtime_ref)
        except Exception:
            return None
        health_url = build_runtime_health_url(runtime_ref)
        manifest = deployment_service._load_manifest(commit_sha, unit_id)
        if not self._healthcheck_passes(manifest.health.path, health_url):
            return None
        return deployment.deployment_id

    def _deployment_is_current(
        self,
        registry: RegistryService,
        deployment_service: DeploymentService,
        unit_id: str,
        commit_sha: str,
    ) -> bool:
        return self._current_deployment_id(registry, deployment_service, unit_id, commit_sha) is not None

    @staticmethod
    def _record_status(
        status_tracker: RuntimeBootstrapStatusTracker | None,
        run_id: int | None,
        result: RuntimeUnitBootstrapResult,
    ) -> None:
        if status_tracker is None or run_id is None:
            return
        if result.status == "current":
            status_tracker.mark_unit_current(run_id, result.unit_id, result.deployment_id)
        elif result.status == "skipped":
            status_tracker.mark_unit_skipped(run_id, result.unit_id, result.failure_reason)
        elif result.status == "healthy":
            status_tracker.mark_unit_healthy(run_id, result.unit_id, result.deployment_id)
        elif result.status == "failed":
            status_tracker.mark_unit_failed(
                run_id,
                result.unit_id,
                result.failure_reason or "runtime bootstrap failed",
                result.deployment_id,
            )
        elif result.status == "blocked":
            status_tracker.mark_unit_blocked(
                run_id,
                result.unit_id,
                result.failure_reason or "runtime bootstrap blocked",
                result.deployment_id,
            )

    @staticmethod
    def _healthcheck_passes(health_path: str, health_url: str) -> bool:
        if not health_url.endswith(health_path):
            return False
        try:
            response = httpx.get(health_url, timeout=5.0)
        except Exception:
            return False
        return response.status_code < 400


class ApplicationRegistryBootstrapper:
    """Seed built-in platform applications into the control-plane registry."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def ensure_seeded(self) -> None:
        session_factory = get_session_factory()
        with session_factory() as session:
            registry = RegistryService(session)
            registry.seed_builtin_applications(self.settings.resolved_apps_source_root)
            session.commit()

"""Managed git operations."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path, PurePosixPath

from app.actors import ActorContext
from app.config import Settings
from app.services.shell import run_command


class ManagedGitRepository:
    """Git-backed managed fork operations."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def list_roots(self) -> list[str]:
        return list(self.settings.allowed_code_roots)

    def list_branches(self) -> list[str]:
        result = run_command(
            [
                "git",
                f"--git-dir={self.settings.bare_repo_dir}",
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads",
            ]
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def ensure_branch_worktree(self, branch: str, *, from_branch: str = "main") -> Path:
        branch = branch.strip()
        if not branch:
            raise ValueError("branch is required")
        if branch == "main":
            return self.settings.main_worktree_dir

        target = self.settings.branch_worktrees_dir / self._branch_slug(branch)
        if target.exists():
            return target

        branch_exists = branch in self.list_branches()
        args = ["git", f"--git-dir={self.settings.bare_repo_dir}", "worktree", "add"]
        if not branch_exists:
            args.extend(["-b", branch])
        args.extend([str(target), branch if branch_exists else from_branch])
        run_command(args)
        return target

    def create_branch(self, branch: str, from_branch: str = "main") -> str:
        self.ensure_branch_worktree(branch, from_branch=from_branch)
        return self.get_head_commit(branch)

    def read_file(self, branch: str, path: str) -> str:
        normalized = self.normalize_code_path(path)
        worktree = self.ensure_branch_worktree(branch)
        return (worktree / normalized).read_text(encoding="utf-8")

    def write_file(self, branch: str, path: str, content: str) -> list[str]:
        normalized = self.normalize_code_path(path)
        worktree = self.ensure_branch_worktree(branch)
        target = worktree / normalized
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return self.get_changed_files(branch)

    def get_tree(self, branch: str, path: str | None = None) -> list[dict[str, str | bool]]:
        worktree = self.ensure_branch_worktree(branch)
        if not path:
            return [{"path": root, "name": Path(root).name, "is_dir": True} for root in self.list_roots()]
        normalized = self.normalize_code_path(path)
        target = worktree / normalized
        if not target.exists():
            raise FileNotFoundError(path)
        if target.is_file():
            return [{"path": normalized.as_posix(), "name": target.name, "is_dir": False}]
        entries = []
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            relative = child.relative_to(worktree).as_posix()
            entries.append({"path": relative, "name": child.name, "is_dir": child.is_dir()})
        return entries

    def get_changed_files(self, branch: str) -> list[str]:
        worktree = self.ensure_branch_worktree(branch)
        result = run_command(["git", "status", "--porcelain"], cwd=worktree)
        files: list[str] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            files.append(line[3:].strip())
        return files

    def get_head_commit(self, branch: str) -> str:
        worktree = self.ensure_branch_worktree(branch)
        return run_command(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()

    def create_commit(self, branch: str, message: str, actor: ActorContext) -> tuple[str, list[str]]:
        worktree = self.ensure_branch_worktree(branch)
        changed_files = self.get_changed_files(branch)
        if not changed_files:
            raise ValueError("no changes to commit")
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": actor.display_name,
                "GIT_AUTHOR_EMAIL": f"{actor.actor_id}@space-ops.local",
                "GIT_COMMITTER_NAME": actor.display_name,
                "GIT_COMMITTER_EMAIL": f"{actor.actor_id}@space-ops.local",
            }
        )
        run_command(["git", "add", "."], cwd=worktree)
        run_command(["git", "commit", "-m", message], cwd=worktree, env=env)
        return self.get_head_commit(branch), changed_files

    def get_history(self, branch: str, path: str | None = None, limit: int = 20) -> list[dict[str, str]]:
        worktree = self.ensure_branch_worktree(branch)
        args = [
            "git",
            "log",
            f"-n{limit}",
            "--pretty=format:%H%x09%an%x09%ct%x09%s",
        ]
        if path:
            args.extend(["--", self.normalize_code_path(path).as_posix()])
        result = run_command(args, cwd=worktree)
        history = []
        for line in result.stdout.splitlines():
            commit_sha, author, committed_at, subject = line.split("\t", 3)
            history.append(
                {
                    "commit_sha": commit_sha,
                    "author": author,
                    "committed_at": committed_at,
                    "subject": subject,
                }
            )
        return history

    def get_diff(
        self,
        branch: str,
        *,
        base_ref: str | None = None,
        head_ref: str = "HEAD",
        path: str | None = None,
    ) -> str:
        worktree = self.ensure_branch_worktree(branch)
        if base_ref is None:
            base_ref = "HEAD~1"
        args = ["git", "diff", f"{base_ref}..{head_ref}"]
        if path:
            args.extend(["--", self.normalize_code_path(path).as_posix()])
        return run_command(args, cwd=worktree).stdout

    def resolve_commit(self, branch: str, commit_sha: str | None = None) -> str:
        if commit_sha:
            return run_command(
                ["git", f"--git-dir={self.settings.bare_repo_dir}", "rev-parse", commit_sha]
            ).stdout.strip()
        return self.get_head_commit(branch)

    def materialize_commit(self, commit_sha: str, target_dir: Path) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        archive = run_command(
            ["git", f"--git-dir={self.settings.bare_repo_dir}", "archive", "--format=tar", commit_sha],
            text=False,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar_stream:
            if hasattr(tarfile, "data_filter"):
                tar_stream.extractall(target_dir, filter="data")
            else:
                tar_stream.extractall(target_dir)

    def normalize_code_path(self, path: str) -> Path:
        candidate = PurePosixPath(path or ".")
        if candidate.is_absolute():
            raise ValueError("absolute paths are not allowed")
        if ".." in candidate.parts:
            raise ValueError("path traversal is not allowed")
        normalized = Path(candidate.as_posix().strip("./"))
        normalized_str = normalized.as_posix()
        if normalized_str == ".":
            raise ValueError("path is required")
        if not any(
            normalized_str == root or normalized_str.startswith(f"{root}/")
            for root in self.settings.allowed_code_roots
        ):
            raise ValueError("path is outside approved managed roots")
        return normalized

    @staticmethod
    def _branch_slug(branch: str) -> str:
        return "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in branch)

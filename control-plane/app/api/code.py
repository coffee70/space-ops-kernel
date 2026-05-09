"""Code operations API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import app.config
from app.db import get_db
from app.deployments.service import DeploymentService
from app.git.repository import ManagedGitRepository
from app.models.runtime import ManagedBranch, utcnow
from app.schemas import BranchCreateRequest, CommitCreateRequest, DeploymentSubmissionRequest, Envelope, FileWriteRequest

router = APIRouter(prefix="/code", tags=["code"])


def get_repository() -> ManagedGitRepository:
    return ManagedGitRepository(app.config.get_settings())


def get_deployment_service(session: Session = Depends(get_db)) -> DeploymentService:
    settings = app.config.get_settings()
    return DeploymentService(settings, ManagedGitRepository(settings), session)


@router.get("/roots", response_model=Envelope)
def get_roots(repository: ManagedGitRepository = Depends(get_repository)) -> Envelope:
    return Envelope(branch="main", commit_sha=repository.get_head_commit("main"), data={"roots": repository.list_roots()})


@router.get("/tree", response_model=Envelope)
def get_tree(
    branch: str = Query("main"),
    path: str | None = Query(None),
    repository: ManagedGitRepository = Depends(get_repository),
) -> Envelope:
    try:
        data_path = path or ""
        return Envelope(
            branch=branch,
            commit_sha=repository.get_head_commit(branch),
            path=data_path or None,
            changed_files=repository.get_changed_files(branch),
            data={"entries": repository.get_tree(branch, path)},
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/file", response_model=Envelope)
def get_file(
    branch: str = Query("main"),
    path: str = Query(...),
    repository: ManagedGitRepository = Depends(get_repository),
) -> Envelope:
    try:
        content = repository.read_file(branch, path)
        return Envelope(
            branch=branch,
            commit_sha=repository.get_head_commit(branch),
            path=path,
            changed_files=repository.get_changed_files(branch),
            data={"content": content},
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/file", response_model=Envelope)
def put_file(
    request: FileWriteRequest,
    repository: ManagedGitRepository = Depends(get_repository),
) -> Envelope:
    try:
        changed_files = repository.write_file(request.branch, request.path, request.content)
        return Envelope(
            branch=request.branch,
            commit_sha=repository.get_head_commit(request.branch),
            path=request.path,
            changed_files=changed_files,
            data={"written": True},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/branches", response_model=Envelope)
def get_branches(repository: ManagedGitRepository = Depends(get_repository)) -> Envelope:
    return Envelope(branch="main", commit_sha=repository.get_head_commit("main"), data={"branches": repository.list_branches()})


@router.post("/branches", response_model=Envelope)
def create_branch(
    request: BranchCreateRequest,
    repository: ManagedGitRepository = Depends(get_repository),
    session: Session = Depends(get_db),
) -> Envelope:
    try:
        branch_exists_before = request.branch in repository.list_branches()
        base_commit_sha = repository.get_head_commit(request.from_branch)
        commit_sha = repository.create_branch(request.branch, request.from_branch)
        worktree = repository.ensure_branch_worktree(request.branch, from_branch=request.from_branch)
        branch_record = session.query(ManagedBranch).filter(ManagedBranch.branch_name == request.branch).one_or_none()
        if branch_record is None:
            branch_record = ManagedBranch(
                branch_name=request.branch,
                repository_root=str(repository.settings.bare_repo_dir),
                worktree_path=str(worktree),
                base_branch=request.from_branch,
                base_commit_sha=base_commit_sha,
                created_commit_sha=commit_sha,
                delete_eligible=not branch_exists_before,
            )
            session.add(branch_record)
        else:
            branch_record.repository_root = str(repository.settings.bare_repo_dir)
            branch_record.worktree_path = str(worktree)
            branch_record.base_branch = request.from_branch
            branch_record.base_commit_sha = base_commit_sha
            branch_record.created_commit_sha = commit_sha
            branch_record.updated_at = utcnow()
        session.flush()
        return Envelope(
            branch=request.branch,
            commit_sha=commit_sha,
            changed_files=[],
            data={
                "created": True,
                "base_branch": request.from_branch,
                "base_commit_sha": base_commit_sha,
                "branch_existed_before": branch_exists_before,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/commits", response_model=Envelope)
def create_commit(
    request: CommitCreateRequest,
    repository: ManagedGitRepository = Depends(get_repository),
) -> Envelope:
    try:
        commit_sha, changed_files = repository.create_commit(request.branch, request.message)
        return Envelope(
            branch=request.branch,
            commit_sha=commit_sha,
            changed_files=changed_files,
            data={"message": request.message},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/history", response_model=Envelope)
def get_history(
    branch: str = Query("main"),
    path: str | None = Query(None),
    repository: ManagedGitRepository = Depends(get_repository),
) -> Envelope:
    return Envelope(
        branch=branch,
        commit_sha=repository.get_head_commit(branch),
        path=path,
        changed_files=repository.get_changed_files(branch),
        data={"history": repository.get_history(branch, path)},
    )


@router.get("/diff", response_model=Envelope)
def get_diff(
    branch: str = Query("main"),
    base_ref: str | None = Query(None),
    head_ref: str = Query("HEAD"),
    path: str | None = Query(None),
    repository: ManagedGitRepository = Depends(get_repository),
) -> Envelope:
    try:
        diff = repository.get_diff(branch, base_ref=base_ref, head_ref=head_ref, path=path)
        return Envelope(
            branch=branch,
            commit_sha=repository.get_head_commit(branch),
            path=path,
            changed_files=repository.get_changed_files(branch),
            data={"diff": diff},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/deployment-submissions")
def submit_deployment_submission(
    request: DeploymentSubmissionRequest,
    service: DeploymentService = Depends(get_deployment_service),
) -> dict:
    try:
        return service.submit(request).model_dump()
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

from __future__ import annotations


def test_bootstrap_roots_and_tree(client) -> None:
    roots = client.get("/code/roots")
    assert roots.status_code == 200
    assert roots.json()["data"]["roots"] == [
        "project/space-ops-platform",
        "project/space-ops-apps",
        "manifests/units",
    ]

    tree = client.get("/code/tree", params={"path": "project/space-ops-platform"})
    assert tree.status_code == 200
    names = [entry["name"] for entry in tree.json()["data"]["entries"]]
    assert "README.md" in names
    assert "backend" in names


def test_path_traversal_rejected(client) -> None:
    response = client.get("/code/file", params={"path": "../secrets.txt"})
    assert response.status_code == 400
    assert "path traversal" in response.json()["detail"]


def test_branch_write_commit_history_and_diff(client) -> None:
    branch_response = client.post("/code/branches", json={"branch": "feature/runtime-registry", "from_branch": "main"})
    assert branch_response.status_code == 200

    file_path = "project/space-ops-platform/README.md"
    write_response = client.put(
        "/code/file",
        json={
            "branch": "feature/runtime-registry",
            "path": file_path,
            "content": "platform\nphase-2\n",
        },
    )
    assert write_response.status_code == 200
    assert file_path in write_response.json()["changed_files"]

    commit_response = client.post(
        "/code/commits",
        json={"branch": "feature/runtime-registry", "message": "Add runtime registry note"},
        headers={"X-Actor-Id": "operator", "X-Actor-Name": "Operator"},
    )
    assert commit_response.status_code == 200
    commit_sha = commit_response.json()["commit_sha"]
    assert len(commit_sha) >= 7

    history_response = client.get("/code/history", params={"branch": "feature/runtime-registry", "path": file_path})
    assert history_response.status_code == 200
    assert history_response.json()["data"]["history"][0]["subject"] == "Add runtime registry note"

    diff_response = client.get("/code/diff", params={"branch": "feature/runtime-registry", "base_ref": "HEAD~1", "path": file_path})
    assert diff_response.status_code == 200
    assert "+phase-2" in diff_response.json()["data"]["diff"]


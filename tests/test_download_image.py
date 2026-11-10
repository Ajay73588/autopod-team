import pytest
from unittest.mock import MagicMock, patch
from docker_manager import DockerManager
from app import app


def test_export_container_image_empty_name():
    dm = DockerManager()
    res = dm.export_container_image("")
    assert res["success"] is False
    assert "cannot be empty" in res["error"]


def test_export_container_image_local_mock(monkeypatch):
    dm = DockerManager()
    monkeypatch.setattr(dm, "get_containers", lambda: [{"container_name": "test-box", "Image": "my-img:v1", "is_worker": False}])
    monkeypatch.setattr(dm, "_run_cmd", lambda cmd, **kwargs: (True, "", ""))
    
    mock_s3 = MagicMock()
    mock_s3.generate_presigned_url.return_value = "https://s3.amazonaws.com/test-box.tar?signed=true"
    
    with patch("boto3.client", return_value=mock_s3):
        res = dm.export_container_image("test-box")
        assert res["success"] is True
        assert res["container_name"] == "test-box"
        assert res["download_url"] == "https://s3.amazonaws.com/test-box.tar?signed=true"
        assert res["filename"].startswith("test-box")
        assert res["filename"].endswith(".tar")


def test_api_download_container_image_unauthorized(monkeypatch):
    import auth
    monkeypatch.setattr(auth, "_ADMIN_KEY", "configured-secret-key")
    client = app.test_client()
    resp = client.post("/api/containers/test-box/download-image")
    assert resp.status_code == 401


def test_api_download_container_image_success(monkeypatch):
    import auth
    admin_key = "test-key"
    monkeypatch.setattr(auth, "_ADMIN_KEY", admin_key)
    client = app.test_client()

    with patch("app.docker_mgr") as mock_mgr:
        mock_mgr.export_container_image.return_value = {
            "success": True,
            "container_name": "test-box",
            "download_url": "https://s3.amazonaws.com/test.tar",
            "filename": "test.tar"
        }
        resp = client.post(
            "/api/containers/test-box/download-image",
            headers={"X-Admin-Key": admin_key}
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["data"]["download_url"] == "https://s3.amazonaws.com/test.tar"

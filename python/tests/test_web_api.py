"""HTTP boundary tests for the local Web workbench."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.web.server import create_app  # noqa: E402
from mini_claude.web.workspace import WorkspaceManager  # noqa: E402


class TestWebApi(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "main.py").write_text("print('ok')\n", encoding="utf-8")
        (self.project / ".env").write_text("SECRET=value\n", encoding="utf-8")
        manager = WorkspaceManager(self.root / "managed")
        self.manager = manager
        self.client = TestClient(create_app(workspace_manager=manager))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_health_static_page_and_open_workspace(self):
        self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})
        self.assertIn("智修 Agent", self.client.get("/").text)

        response = self.client.post("/api/workspaces/open", json={"path": str(self.project)})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["path"] for item in payload["files"]], ["main.py"])
        self.assertEqual(payload["workspace_mode"], "source")

    def test_native_picker_mounts_source_directory_and_allows_cancellation(self):
        with patch(
            "mini_claude.web.server._pick_local_directory",
            return_value=str(self.project),
        ):
            response = self.client.post("/api/workspaces/pick")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["workspace_mode"], "source")
        self.assertEqual(Path(response.json()["root"]), self.project)

        with patch("mini_claude.web.server._pick_local_directory", return_value=None):
            cancelled = self.client.post("/api/workspaces/pick")
        self.assertEqual(cancelled.json(), {"cancelled": True})

    def test_sensitive_file_cannot_be_read_through_api(self):
        workspace = self.client.post(
            "/api/workspaces/open", json={"path": str(self.project)}
        ).json()
        response = self.client.get(
            f"/api/workspaces/{workspace['id']}/file", params={"path": ".env"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("受保护", response.json()["detail"])

    def test_config_never_returns_api_key(self):
        payload = self.client.get("/api/config").json()
        self.assertNotIn("api_key", payload)
        self.assertNotIn("api_base", payload)

    def test_mounted_workspace_can_remove_agent_access_without_deleting_original(self):
        workspace = self.client.post(
            "/api/workspaces/open", json={"path": str(self.project)}
        ).json()
        response = self.client.post(
            f"/api/workspaces/{workspace['id']}/access/remove",
            json={"paths": ["main.py"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["removed"], 1)
        self.assertTrue((self.project / "main.py").exists())
        tree = self.client.get(f"/api/workspaces/{workspace['id']}/tree").json()
        self.assertEqual(tree["files"], [])
        read = self.client.get(
            f"/api/workspaces/{workspace['id']}/file", params={"path": "main.py"}
        )
        self.assertEqual(read.status_code, 400)

    def test_imported_copy_access_can_be_removed_with_scoped_path(self):
        import base64

        workspace = self.client.post("/api/workspaces/import", json={
            "name": "api-delete",
            "files": [{
                "path": "main.py",
                "content_base64": base64.b64encode(b"print('ok')\n").decode(),
            }],
        }).json()
        response = self.client.post(
            f"/api/workspaces/{workspace['id']}/access/remove",
            json={"paths": ["main.py"]},
        )
        self.assertEqual(response.status_code, 200)

        blocked = self.client.post(
            f"/api/workspaces/{workspace['id']}/access/remove",
            json={"paths": ["../outside.py"]},
        )
        self.assertEqual(blocked.status_code, 400)

    def test_undo_endpoint_restores_last_agent_checkpoint(self):
        workspace = self.client.post(
            "/api/workspaces/open", json={"path": str(self.project)}
        ).json()
        self.manager.create_checkpoint(workspace["id"])
        (self.project / "main.py").write_text("print('broken')\n", encoding="utf-8")
        self.assertTrue(self.manager.finalize_checkpoint(workspace["id"]))

        response = self.client.post(f"/api/workspaces/{workspace['id']}/undo")

        self.assertEqual(response.status_code, 200)
        self.assertEqual((self.project / "main.py").read_text(), "print('ok')\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)

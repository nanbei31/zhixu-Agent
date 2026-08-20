"""FastAPI application and CLI launcher for the local Web workbench."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .runtime import SessionManager, resolve_model_config
from .workspace import WorkspaceManager


STATIC_DIR = Path(__file__).parent / "static"


class OpenWorkspaceRequest(BaseModel):
    path: str


class ImportWorkspaceRequest(BaseModel):
    name: str = "workspace"
    files: list[dict] = Field(default_factory=list)


class WriteFileRequest(BaseModel):
    content: str


class RemoveFilesRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)


class ClearWorkspaceRequest(BaseModel):
    confirmation: str


class CreateSessionRequest(BaseModel):
    workspace_id: str
    allow_shell: bool = False


class MessageRequest(BaseModel):
    message: str


class ConfirmRequest(BaseModel):
    confirmation_id: str
    approved: bool


def create_app(
    workspace_manager: WorkspaceManager | None = None,
    session_manager: SessionManager | None = None,
) -> FastAPI:
    workspaces = workspace_manager or WorkspaceManager()
    sessions = session_manager or SessionManager(workspaces)
    app = FastAPI(title="智修 Agent", docs_url="/api/docs", redoc_url=None)
    app.state.workspaces = workspaces
    app.state.sessions = sessions

    def fail(exc: Exception, status: int = 400):
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/config")
    async def config():
        model = resolve_model_config()
        return {key: value for key, value in model.items() if key not in {"api_key", "api_base", "anthropic_base_url"}}

    @app.post("/api/workspaces/open")
    async def open_workspace(body: OpenWorkspaceRequest):
        try:
            workspace = workspaces.open_path(body.path)
            return _workspace_payload(workspace, workspaces)
        except Exception as exc:
            fail(exc)

    @app.post("/api/workspaces/pick")
    async def pick_workspace():
        try:
            raw_path = await asyncio.to_thread(_pick_local_directory)
            if not raw_path:
                return {"cancelled": True}
            workspace = workspaces.open_path(raw_path)
            return _workspace_payload(workspace, workspaces)
        except Exception as exc:
            fail(exc)

    @app.post("/api/workspaces/import")
    async def import_workspace(body: ImportWorkspaceRequest):
        try:
            workspace = workspaces.import_files(body.name, body.files)
            return _workspace_payload(workspace, workspaces)
        except Exception as exc:
            fail(exc)

    @app.get("/api/workspaces/{workspace_id}/tree")
    async def workspace_tree(workspace_id: str):
        try:
            return {"files": workspaces.tree(workspace_id)}
        except Exception as exc:
            fail(exc, 404 if isinstance(exc, KeyError) else 400)

    @app.get("/api/workspaces/{workspace_id}/file")
    async def read_file(workspace_id: str, path: str = Query(...)):
        try:
            return workspaces.read_file(workspace_id, path)
        except Exception as exc:
            fail(exc, 404 if isinstance(exc, (KeyError, FileNotFoundError)) else 400)

    @app.put("/api/workspaces/{workspace_id}/file")
    async def write_file(workspace_id: str, body: WriteFileRequest, path: str = Query(...)):
        try:
            return workspaces.write_file(workspace_id, path, body.content)
        except Exception as exc:
            fail(exc, 404 if isinstance(exc, KeyError) else 400)

    @app.post("/api/workspaces/{workspace_id}/access/remove")
    async def remove_workspace_access(workspace_id: str, body: RemoveFilesRequest):
        try:
            return workspaces.remove_access(workspace_id, body.paths)
        except Exception as exc:
            fail(exc, 404 if isinstance(exc, (KeyError, FileNotFoundError)) else 400)

    @app.post("/api/workspaces/{workspace_id}/access/clear")
    async def clear_workspace_access(workspace_id: str, body: ClearWorkspaceRequest):
        try:
            return workspaces.clear_access(workspace_id, body.confirmation)
        except Exception as exc:
            fail(exc, 404 if isinstance(exc, KeyError) else 400)

    @app.get("/api/workspaces/{workspace_id}/diff")
    async def workspace_diff(workspace_id: str):
        try:
            return workspaces.diff(workspace_id)
        except Exception as exc:
            fail(exc, 404 if isinstance(exc, KeyError) else 400)

    @app.post("/api/workspaces/{workspace_id}/undo")
    async def undo_workspace(workspace_id: str):
        try:
            return workspaces.undo_last_change(workspace_id)
        except Exception as exc:
            fail(exc, 404 if isinstance(exc, KeyError) else 400)

    @app.post("/api/sessions")
    async def create_session(body: CreateSessionRequest):
        try:
            session = sessions.create(body.workspace_id, allow_shell=body.allow_shell)
            return {"session_id": session.id, "allow_shell": session.allow_shell}
        except Exception as exc:
            fail(exc)

    @app.post("/api/sessions/{session_id}/messages", status_code=202)
    async def send_message(session_id: str, body: MessageRequest):
        try:
            return sessions.send(session_id, body.message)
        except Exception as exc:
            fail(exc, 409 if isinstance(exc, RuntimeError) else 400)

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(
        request: Request,
        session_id: str,
        after: int = Query(0, ge=0),
    ):
        try:
            session = sessions.get(session_id)
        except Exception as exc:
            fail(exc, 404)

        async def stream():
            for event in session.bus.after(after):
                yield _sse(event)
            queue = session.bus.subscribe()
            try:
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield _sse(event)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                session.bus.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    @app.post("/api/sessions/{session_id}/confirm")
    async def confirm(session_id: str, body: ConfirmRequest):
        try:
            sessions.confirm(session_id, body.confirmation_id, body.approved)
            return {"ok": True}
        except Exception as exc:
            fail(exc)

    @app.post("/api/sessions/{session_id}/abort")
    async def abort(session_id: str):
        try:
            sessions.abort(session_id)
            return {"ok": True}
        except Exception as exc:
            fail(exc, 404)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


def _workspace_payload(workspace, manager: WorkspaceManager) -> dict:
    return {
        "id": workspace.id,
        "name": workspace.name,
        "root": str(workspace.root),
        "managed": workspace.managed,
        "workspace_mode": "copy" if workspace.managed else "source",
        "files": manager.tree(workspace.id),
    }


def _pick_local_directory() -> str | None:
    """Open a native directory chooser on the same machine as the server."""
    if sys.platform != "win32":
        raise RuntimeError("当前系统不支持原生目录选择，请使用“输入路径”挂载本地目录")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$dialog.Description = '选择要由智修 Agent 直接修改的源代码目录'; "
        "$dialog.ShowNewFolderButton = $false; "
        "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Write($dialog.SelectedPath) }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Sta", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    if result.returncode:
        message = result.stderr.strip() or "无法打开本地目录选择框"
        raise RuntimeError(message)
    selected = result.stdout.strip()
    return selected or None


def _sse(event: dict) -> str:
    return f"id: {event['sequence']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def run_web(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = True) -> None:
    import uvicorn

    url = f"http://{host}:{port}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"智修 Agent Web: {url}")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")

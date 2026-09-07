from __future__ import annotations

bl_info = {
    "name": "FolderBridge Blender Bridge",
    "author": "OpenAI / FolderBridge",
    "version": (0, 1, 1),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > FolderBridge",
    "description": "Loopback-only authenticated bridge for the FolderBridge Blender Toolkit.",
    "category": "System",
}

import base64
import hmac
import json
import os
import queue
import tempfile
import threading
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import bpy

PORT = 8766
HOST = "127.0.0.1"
TOKEN_FILE = Path(__file__).resolve().with_name("bridge-token.txt")
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 24 * 60 * 60

_SERVER = None
_SERVER_THREAD = None
_REQUESTS: "queue.Queue[PendingRequest]" = queue.Queue()
_TIMER_REGISTERED = False

SAFE_OPERATOR_PREFIXES = {
    "object", "mesh", "curve", "armature", "pose", "transform", "view3d", "sculpt",
    "paint", "geometry", "node", "anim", "nla", "constraint", "marker", "rigidbody",
    "uv", "ed", "screen", "grease_pencil", "gpencil", "sequencer", "clip", "mask",
    "fluid", "ptcache", "boid", "particle", "lattice",
}
DANGEROUS_OPERATOR_TOKENS = {
    "open", "save", "import", "export", "script", "python", "url", "path", "file",
    "addon", "extension", "install", "quit", "console", "recover", "factory",
}
PATHLIKE_KEYS = {
    "path", "filepath", "directory", "filename", "url", "script", "command", "executable",
    "python", "module",
}
COMFYUI_PANEL_TYPES = [
    "ComfyBlenderPanelFileBrowser",
    "ComfyBlenderPanelInput3DViewer",
    "ComfyBlenderPanelInputImageEditor",
    "ComfyBlenderPanelOutput3DViewer",
    "ComfyBlenderPanelOutputImageEditor",
    "ComfyBlenderPanelPaintMask",
    "ComfyBlenderPanelWorkflow3DViewer",
    "ComfyBlenderPanelWorkflowImageEditor",
]


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.retryable = retryable


@dataclass
class PendingRequest:
    action: str
    params: dict[str, Any]
    workspace_root: str
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def _token() -> str:
    try:
        value = TOKEN_FILE.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise BridgeError("BRIDGE_TOKEN_MISSING", "bridge-token.txt is missing. Re-run Blender Toolkit install.ps1.") from exc
    if len(value) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in value):
        raise BridgeError("BRIDGE_TOKEN_INVALID", "bridge-token.txt is invalid.")
    return value


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES:
        status = 500
        body = b'{"ok":false,"error":{"code":"BRIDGE_RESPONSE_TOO_LARGE","message":"Response exceeded 32 MiB."}}'
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "null")
    handler.end_headers()
    handler.wfile.write(body)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "FolderBridgeBlender/0.1"

    def do_POST(self) -> None:
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            _json_response(self, 403, {"ok": False, "error": {"code": "LOOPBACK_ONLY", "message": "Loopback only."}})
            return
        try:
            expected = _token()
        except BridgeError as exc:
            _json_response(self, 503, {"ok": False, "error": _error_dict(exc)})
            return
        received = self.headers.get("X-FolderBridge-Blender-Token", "")
        if not hmac.compare_digest(received, expected):
            _json_response(self, 401, {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "Invalid bridge token."}})
            return
        if self.path != "/rpc":
            _json_response(self, 404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "Unknown route."}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 2 or length > MAX_BODY_BYTES:
            _json_response(self, 413, {"ok": False, "error": {"code": "BAD_BODY_SIZE", "message": "Request body size is invalid."}})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            _json_response(self, 400, {"ok": False, "error": {"code": "BAD_JSON", "message": "Invalid JSON request."}})
            return
        if not isinstance(payload, dict):
            _json_response(self, 400, {"ok": False, "error": {"code": "BAD_REQUEST", "message": "Request must be an object."}})
            return
        action = payload.get("action")
        params = payload.get("params", {})
        workspace_root = payload.get("workspace_root", "")
        if not isinstance(action, str) or not isinstance(params, dict) or not isinstance(workspace_root, str):
            _json_response(self, 400, {"ok": False, "error": {"code": "BAD_REQUEST", "message": "Invalid action envelope."}})
            return

        pending = PendingRequest(action=action, params=params, workspace_root=workspace_root)
        _REQUESTS.put(pending)
        if not pending.done.wait(REQUEST_TIMEOUT_SECONDS):
            _json_response(self, 504, {"ok": False, "error": {"code": "BRIDGE_TIMEOUT", "message": "Blender main-thread action timed out.", "retryable": True}})
            return
        if pending.error:
            _json_response(self, 400, {"ok": False, "error": pending.error})
        else:
            _json_response(self, 200, {"ok": True, "result": pending.result or {}})

    def log_message(self, format: str, *args: object) -> None:
        return


def _error_dict(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, BridgeError):
        return {
            "code": exc.code,
            "message": str(exc),
            "details": exc.details,
            "retryable": exc.retryable,
        }
    return {
        "code": "BLENDER_EXCEPTION",
        "message": str(exc)[:2048],
        "details": {"traceback": traceback.format_exc(limit=8)[-8192:]},
        "retryable": False,
    }


def _drain_requests() -> float:
    processed = 0
    while processed < 8:
        try:
            pending = _REQUESTS.get_nowait()
        except queue.Empty:
            break
        try:
            pending.result = _dispatch(pending.action, pending.params, pending.workspace_root)
        except BaseException as exc:
            pending.error = _error_dict(exc)
        finally:
            pending.done.set()
        processed += 1
    return 0.05


def _dispatch(action: str, p: dict[str, Any], workspace_root: str) -> dict[str, Any]:
    if action == "status":
        return _status()
    root = _workspace_root(workspace_root)
    # Opening a workspace-confined .blend is the explicit action that is allowed
    # to switch the live editor away from a file belonging to another project.
    # All other live actions remain pinned to the selected workspace.
    if action != "open-blend":
        _assert_current_file_in_workspace(root)

    if action == "inspect-context":
        return _inspect_context(bool(p.get("include_addons", True)))
    if action == "list-data":
        return _list_data(p["kind"], int(p.get("offset", 0)), int(p.get("limit", 100)))
    if action == "inspect-data":
        return _inspect_data(p["kind"], p["name"], p.get("properties"))
    if action == "screenshot":
        return _screenshot(int(p.get("max_width", 960)))
    if action == "set-context":
        return _set_context(p)
    if action == "create-object":
        return _create_object(p)
    if action == "delete-object":
        return _delete_object(p)
    if action == "create-data":
        return _create_data(p)
    if action == "remove-data":
        return _remove_data(p)
    if action == "load-image":
        return _load_image(root, p)
    if action == "set-properties":
        return _set_properties(p)
    if action == "set-transform":
        return _set_transform(p)
    if action == "mesh-set-geometry":
        return _mesh_set_geometry(p)
    if action == "collection-create":
        return _collection_create(p)
    if action == "collection-link":
        return _collection_link(p)
    if action == "modifier-add":
        return _modifier_add(p)
    if action == "modifier-remove":
        return _modifier_remove(p)
    if action == "constraint-add":
        return _constraint_add(p)
    if action == "constraint-remove":
        return _constraint_remove(p)
    if action == "node-create":
        return _node_create(p)
    if action == "node-remove":
        return _node_remove(p)
    if action == "node-set":
        return _node_set(p)
    if action == "node-link":
        return _node_link(p)
    if action == "node-unlink":
        return _node_unlink(p)
    if action == "node-interface-socket":
        return _node_interface_socket(p)
    if action == "keyframe-insert":
        return _keyframe(p, insert=True)
    if action == "keyframe-delete":
        return _keyframe(p, insert=False)
    if action == "set-frame":
        bpy.context.scene.frame_set(int(p["frame"]), subframe=float(p.get("subframe", 0.0)))
        return {"frame": bpy.context.scene.frame_current, "subframe": bpy.context.scene.frame_subframe}
    if action == "operator-call":
        return _operator_call(p)
    if action == "undo":
        return {"result": sorted(bpy.ops.ed.undo())}
    if action == "redo":
        return {"result": sorted(bpy.ops.ed.redo())}
    if action == "open-blend":
        return _open_blend(root, p)
    if action == "save-blend":
        return _save_blend(root, p)
    if action == "import-asset":
        return _import_asset(root, p)
    if action == "export-asset":
        return _export_asset(root, p)
    if action == "render-still":
        return _render_still(root, p)
    if action == "render-animation":
        return _render_animation(root, p)
    raise BridgeError("UNSUPPORTED_ACTION", f"Unsupported Blender action: {action}")


def _status() -> dict[str, Any]:
    prefs = bpy.context.preferences
    enabled = "comfyui_blender" in prefs.addons
    panels = [name for name in COMFYUI_PANEL_TYPES if hasattr(bpy.types, name)]
    scene_has_settings = hasattr(bpy.context.scene, "comfyui_project_settings") if bpy.context.scene else False
    return {
        "bridge_version": "0.1.1",
        "blender_version": bpy.app.version_string,
        "blender_version_tuple": list(bpy.app.version),
        "current_file": bpy.data.filepath,
        "scene": bpy.context.scene.name if bpy.context.scene else None,
        "mode": bpy.context.mode,
        "comfyui_blender_enabled": enabled,
        "comfyui_panels_registered": panels,
        "comfyui_n_panel_present": bool(enabled and panels and scene_has_settings),
        "comfyui_scene_settings_registered": bool(scene_has_settings),
        "port": PORT,
    }


def _workspace_root(raw: str) -> Path:
    if not raw:
        raise BridgeError("WORKSPACE_REQUIRED", "A selected FolderBridge workspace is required.")
    root = Path(raw).resolve()
    if not root.is_dir():
        raise BridgeError("WORKSPACE_INVALID", "Workspace root is unavailable.")
    return root


def _assert_current_file_in_workspace(root: Path) -> None:
    current = bpy.data.filepath
    if not current:
        return
    path = Path(current).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise BridgeError(
            "BLENDER_WORKSPACE_MISMATCH",
            "The currently open .blend file is outside the selected FolderBridge workspace.",
            details={"current_file": str(path), "workspace_root": str(root)},
        ) from exc


def _workspace_path(root: Path, rel: str, *, must_exist: bool = False) -> Path:
    if not isinstance(rel, str) or not rel or "\\" in rel or rel.startswith("/") or ":" in rel:
        raise BridgeError("INVALID_PATH", "Path must be a POSIX-style workspace-relative path.")
    parts = rel.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BridgeError("INVALID_PATH", "Path contains an invalid component.")
    path = root.joinpath(*parts).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise BridgeError("INVALID_PATH", "Path escapes the selected workspace.") from exc
    if must_exist and not path.exists():
        raise BridgeError("PATH_NOT_FOUND", f"Workspace path does not exist: {rel}")
    return path


def _safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value if not isinstance(value, str) else value[:4096]
    if isinstance(value, (list, tuple)):
        return [_safe(v, depth=depth + 1) for v in list(value)[:64]]
    if hasattr(value, "to_list"):
        try:
            return _safe(value.to_list(), depth=depth + 1)
        except Exception:
            pass
    if hasattr(value, "name") and hasattr(value, "bl_rna"):
        return {"name": getattr(value, "name", ""), "rna_type": value.bl_rna.identifier}
    return str(value)[:4096]


def _inspect_context(include_addons: bool) -> dict[str, Any]:
    scene = bpy.context.scene
    obj = bpy.context.active_object
    data = {
        "scene": scene.name if scene else None,
        "active_object": obj.name if obj else None,
        "selected_objects": [o.name for o in bpy.context.selected_objects[:256]],
        "mode": bpy.context.mode,
        "frame": scene.frame_current if scene else None,
        "render_engine": scene.render.engine if scene else None,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage] if scene else None,
        "camera": scene.camera.name if scene and scene.camera else None,
        "current_file": bpy.data.filepath,
    }
    if include_addons:
        data["enabled_addons"] = sorted(list(bpy.context.preferences.addons.keys()))[:512]
    return data


DATA_COLLECTION_ATTRS = {
    "objects": "objects",
    "collections": "collections",
    "materials": "materials",
    "meshes": "meshes",
    "curves": "curves",
    "cameras": "cameras",
    "lights": "lights",
    "armatures": "armatures",
    "actions": "actions",
    "images": "images",
    "node_groups": "node_groups",
    "worlds": "worlds",
    "scenes": "scenes",
}
DATA_KIND_TO_ATTR = {
    "object": "objects",
    "collection": "collections",
    "material": "materials",
    "mesh": "meshes",
    "curve": "curves",
    "camera": "cameras",
    "light": "lights",
    "armature": "armatures",
    "action": "actions",
    "image": "images",
    "node_group": "node_groups",
    "world": "worlds",
    "scene": "scenes",
}


def _data_collection_from_attr(attr: str):
    collection = getattr(bpy.data, attr, None)
    if collection is None:
        raise BridgeError("BLENDER_DATA_UNAVAILABLE", f"Blender data collection is unavailable: {attr}")
    return collection


def _list_data(kind: str, offset: int, limit: int) -> dict[str, Any]:
    attr = DATA_COLLECTION_ATTRS.get(kind)
    if attr is None:
        raise BridgeError("BAD_DATA_KIND", f"Unsupported data kind: {kind}")
    collection = _data_collection_from_attr(attr)
    items = list(collection)
    page = items[offset:offset + limit]
    return {
        "kind": kind,
        "offset": offset,
        "limit": limit,
        "total": len(items),
        "items": [{"name": item.name, "type": item.bl_rna.identifier, "users": getattr(item, "users", None)} for item in page],
    }


def _inspect_data(kind: str, name: str, properties: Any) -> dict[str, Any]:
    attr = DATA_KIND_TO_ATTR.get(kind)
    if attr is None:
        raise BridgeError("BAD_DATA_KIND", f"Unsupported data kind: {kind}")
    collection = _data_collection_from_attr(attr)
    item = collection.get(name)
    if item is None:
        raise BridgeError("DATA_NOT_FOUND", f"{kind} not found: {name}")
    result = {"name": item.name, "type": item.bl_rna.identifier}
    props = properties if isinstance(properties, list) and properties else [
        prop.identifier for prop in item.bl_rna.properties if not prop.is_readonly and prop.identifier not in {"rna_type"}
    ][:64]
    values = {}
    for prop in props[:64]:
        if not isinstance(prop, str) or prop.startswith("_"):
            continue
        try:
            values[prop] = _safe(getattr(item, prop))
        except Exception as exc:
            values[prop] = {"error": str(exc)[:256]}
    result["properties"] = values
    return result


def _screenshot(max_width: int) -> dict[str, Any]:
    path = Path(tempfile.gettempdir()) / f"folderbridge_blender_{os.getpid()}_{threading.get_ident()}.png"
    try:
        bpy.ops.screen.screenshot(filepath=str(path))
        data = path.read_bytes()
    except Exception as exc:
        raise BridgeError("SCREENSHOT_FAILED", f"Blender screenshot failed: {exc}") from exc
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return {
        "mime_type": "image/png",
        "image_base64": base64.b64encode(data).decode("ascii"),
        "captured_bytes": len(data),
        "requested_max_width": max_width,
    }


def _set_context(p: dict[str, Any]) -> dict[str, Any]:
    mode = p.get("mode", "OBJECT")
    if bpy.context.mode != "OBJECT" and mode != bpy.context.mode:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for name in p.get("selected_objects") or []:
        obj = bpy.data.objects.get(name)
        if obj:
            obj.select_set(True)
    active_name = p.get("active_object") or ""
    if active_name:
        obj = bpy.data.objects.get(active_name)
        if obj is None:
            raise BridgeError("OBJECT_NOT_FOUND", f"Object not found: {active_name}")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
    if mode != "OBJECT":
        bpy.ops.object.mode_set(mode=mode)
    return _inspect_context(False)


def _create_object(p: dict[str, Any]) -> dict[str, Any]:
    name = p["name"]
    if bpy.data.objects.get(name):
        raise BridgeError("OBJECT_EXISTS", f"Object already exists: {name}")
    obj_type = p["object_type"]
    data_name = p.get("data_name") or f"{name}_Data"
    data = None
    if obj_type == "MESH":
        data = bpy.data.meshes.new(data_name)
    elif obj_type == "CURVE":
        data = bpy.data.curves.new(data_name, "CURVE")
        data.dimensions = "3D"
    elif obj_type == "CAMERA":
        data = bpy.data.cameras.new(data_name)
    elif obj_type == "LIGHT":
        data = bpy.data.lights.new(data_name, "POINT")
    elif obj_type == "ARMATURE":
        data = bpy.data.armatures.new(data_name)
    elif obj_type == "FONT":
        data = bpy.data.curves.new(data_name, "FONT")
    elif obj_type != "EMPTY":
        raise BridgeError("BAD_OBJECT_TYPE", f"Unsupported object type: {obj_type}")
    obj = bpy.data.objects.new(name, data)
    collection_name = p.get("collection") or ""
    collection = bpy.data.collections.get(collection_name) if collection_name else bpy.context.scene.collection
    if collection is None:
        raise BridgeError("COLLECTION_NOT_FOUND", f"Collection not found: {collection_name}")
    collection.objects.link(obj)
    if "location" in p:
        obj.location = p["location"]
    if "rotation_euler" in p:
        obj.rotation_euler = p["rotation_euler"]
    if "scale" in p:
        obj.scale = p["scale"]
    return {"name": obj.name, "type": obj.type, "data": obj.data.name if obj.data else None}


def _delete_object(p: dict[str, Any]) -> dict[str, Any]:
    obj = bpy.data.objects.get(p["name"])
    if obj is None:
        raise BridgeError("OBJECT_NOT_FOUND", f"Object not found: {p['name']}")
    data = obj.data
    name = obj.name
    bpy.data.objects.remove(obj, do_unlink=True)
    purged = False
    if p.get("purge_data") and data is not None and getattr(data, "users", 1) == 0:
        coll = _collection_for_datablock(data)
        if coll is not None:
            coll.remove(data)
            purged = True
    return {"deleted": name, "purged_data": purged}


def _collection_for_datablock(data: Any):
    mapping = [
        ("Mesh", bpy.data.meshes), ("Curve", bpy.data.curves), ("Camera", bpy.data.cameras),
        ("Light", bpy.data.lights), ("Armature", bpy.data.armatures),
    ]
    ident = getattr(getattr(data, "bl_rna", None), "identifier", "")
    for type_name, collection in mapping:
        if ident == type_name:
            return collection
    return None


def _create_data(p: dict[str, Any]) -> dict[str, Any]:
    kind, name = p["kind"], p["name"]
    if kind == "material":
        item = bpy.data.materials.new(name)
    elif kind == "mesh":
        item = bpy.data.meshes.new(name)
    elif kind == "curve":
        item = bpy.data.curves.new(name, p.get("subtype") or "CURVE")
    elif kind == "camera":
        item = bpy.data.cameras.new(name)
    elif kind == "light":
        item = bpy.data.lights.new(name, p.get("subtype") or "POINT")
    elif kind == "armature":
        item = bpy.data.armatures.new(name)
    elif kind == "world":
        item = bpy.data.worlds.new(name)
    elif kind == "node_group":
        item = bpy.data.node_groups.new(name, p.get("subtype") or "GeometryNodeTree")
    elif kind == "action":
        item = bpy.data.actions.new(name)
    elif kind == "collection":
        item = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(item)
    else:
        raise BridgeError("BAD_DATA_KIND", f"Unsupported create-data kind: {kind}")
    return {"name": item.name, "type": item.bl_rna.identifier}


def _remove_data(p: dict[str, Any]) -> dict[str, Any]:
    kind = p["kind"]
    attr = DATA_KIND_TO_ATTR.get(kind)
    if attr is None:
        raise BridgeError("BAD_DATA_KIND", f"Unsupported remove-data kind: {kind}")
    collection = _data_collection_from_attr(attr)
    item = collection.get(p["name"])
    if item is None:
        raise BridgeError("DATA_NOT_FOUND", f"{kind} not found: {p['name']}")
    collection.remove(item, do_unlink=bool(p.get("do_unlink", False)))
    return {"removed": p["name"], "kind": kind}


def _load_image(root: Path, p: dict[str, Any]) -> dict[str, Any]:
    path = _workspace_path(root, p["path"], must_exist=True)
    image = bpy.data.images.load(str(path), check_existing=bool(p.get("check_existing", True)))
    if p.get("name"):
        image.name = p["name"]
    return {"name": image.name, "size": list(image.size), "source": image.source}


def _resolve_target(p: dict[str, Any]) -> Any:
    target = p["target"]
    name = p.get("name") or ""
    sub = p.get("subname") or ""
    if target == "scene":
        return bpy.data.scenes.get(name) if name else bpy.context.scene
    if target == "world":
        return bpy.data.worlds.get(name) if name else bpy.context.scene.world
    if target == "object":
        return bpy.data.objects.get(name)
    if target == "object_data":
        obj = bpy.data.objects.get(name)
        return obj.data if obj else None
    if target == "material":
        return bpy.data.materials.get(name)
    if target == "camera":
        return bpy.data.cameras.get(name)
    if target == "light":
        return bpy.data.lights.get(name)
    if target == "image":
        return bpy.data.images.get(name)
    if target == "collection":
        return bpy.data.collections.get(name)
    if target == "modifier":
        obj = bpy.data.objects.get(name)
        return obj.modifiers.get(sub) if obj else None
    if target == "constraint":
        obj = bpy.data.objects.get(name)
        return obj.constraints.get(sub) if obj else None
    return None


def _resolve_ref(value: Any) -> Any:
    if not isinstance(value, dict) or set(value) != {"$ref"}:
        return value
    ref = value["$ref"]
    if not isinstance(ref, dict):
        raise BridgeError("BAD_REFERENCE", "$ref must be an object.")
    kind = ref.get("kind")
    name = ref.get("name")
    if not isinstance(name, str):
        raise BridgeError("BAD_REFERENCE", "$ref.name must be a string.")
    attr = DATA_KIND_TO_ATTR.get(str(kind))
    if attr is None:
        raise BridgeError("BAD_REFERENCE", f"Unsupported reference kind: {kind}")
    collection = _data_collection_from_attr(attr)
    item = collection.get(name)
    if item is None:
        raise BridgeError("BAD_REFERENCE", f"Referenced {kind} not found: {name}")
    return item


def _apply_properties(target: Any, properties: dict[str, Any]) -> dict[str, Any]:
    if target is None:
        raise BridgeError("TARGET_NOT_FOUND", "Blender target was not found.")
    changed = {}
    for key, value in properties.items():
        if not isinstance(key, str) or not key or key.startswith("_") or key in PATHLIKE_KEYS:
            raise BridgeError("PROPERTY_DENIED", f"Property is denied: {key}")
        if "." in key or "[" in key or "]" in key:
            raise BridgeError("PROPERTY_DENIED", "Nested/expression property names are not accepted.")
        if not hasattr(target, key):
            raise BridgeError("PROPERTY_NOT_FOUND", f"Property not found: {key}")
        prop = target.bl_rna.properties.get(key) if hasattr(target, "bl_rna") else None
        if prop is not None and prop.is_readonly:
            raise BridgeError("PROPERTY_READ_ONLY", f"Property is read-only: {key}")
        setattr(target, key, _resolve_ref(value))
        changed[key] = _safe(getattr(target, key))
    return changed


def _set_properties(p: dict[str, Any]) -> dict[str, Any]:
    target = _resolve_target(p)
    return {"changed": _apply_properties(target, p["properties"])}


def _set_transform(p: dict[str, Any]) -> dict[str, Any]:
    obj = bpy.data.objects.get(p["name"])
    if obj is None:
        raise BridgeError("OBJECT_NOT_FOUND", f"Object not found: {p['name']}")
    changed = {}
    for key in ("location", "rotation_euler", "scale"):
        if key in p:
            setattr(obj, key, p[key])
            changed[key] = list(getattr(obj, key))
    return {"name": obj.name, "changed": changed}


def _mesh_set_geometry(p: dict[str, Any]) -> dict[str, Any]:
    obj = bpy.data.objects.get(p["object"])
    if obj is None or obj.type != "MESH":
        raise BridgeError("MESH_OBJECT_REQUIRED", f"Mesh object not found: {p['object']}")
    mesh = obj.data
    mesh.clear_geometry()
    mesh.from_pydata(p.get("vertices") or [], p.get("edges") or [], p.get("faces") or [])
    mesh.update(calc_edges=True)
    if p.get("validate", True):
        mesh.validate(verbose=False, clean_customdata=False)
    return {"object": obj.name, "vertices": len(mesh.vertices), "edges": len(mesh.edges), "polygons": len(mesh.polygons)}


def _collection_create(p: dict[str, Any]) -> dict[str, Any]:
    if bpy.data.collections.get(p["name"]):
        raise BridgeError("COLLECTION_EXISTS", f"Collection already exists: {p['name']}")
    coll = bpy.data.collections.new(p["name"])
    parent_name = p.get("parent") or ""
    parent = bpy.data.collections.get(parent_name) if parent_name else bpy.context.scene.collection
    if parent is None:
        raise BridgeError("COLLECTION_NOT_FOUND", f"Parent collection not found: {parent_name}")
    parent.children.link(coll)
    return {"name": coll.name}


def _collection_link(p: dict[str, Any]) -> dict[str, Any]:
    obj = bpy.data.objects.get(p["object"])
    coll = bpy.data.collections.get(p["collection"])
    if obj is None:
        raise BridgeError("OBJECT_NOT_FOUND", f"Object not found: {p['object']}")
    if coll is None:
        raise BridgeError("COLLECTION_NOT_FOUND", f"Collection not found: {p['collection']}")
    if p.get("unlink_other_collections"):
        for owner in list(obj.users_collection):
            owner.objects.unlink(obj)
    if obj.name not in coll.objects:
        coll.objects.link(obj)
    return {"object": obj.name, "collections": [c.name for c in obj.users_collection]}


def _modifier_add(p: dict[str, Any]) -> dict[str, Any]:
    obj = bpy.data.objects.get(p["object"])
    if obj is None:
        raise BridgeError("OBJECT_NOT_FOUND", f"Object not found: {p['object']}")
    if obj.modifiers.get(p["name"]):
        raise BridgeError("MODIFIER_EXISTS", f"Modifier already exists: {p['name']}")
    mod = obj.modifiers.new(p["name"], p["type"])
    changed = _apply_properties(mod, p.get("properties") or {})
    return {"object": obj.name, "modifier": mod.name, "type": mod.type, "changed": changed}


def _modifier_remove(p: dict[str, Any]) -> dict[str, Any]:
    obj = bpy.data.objects.get(p["object"])
    mod = obj.modifiers.get(p["name"]) if obj else None
    if mod is None:
        raise BridgeError("MODIFIER_NOT_FOUND", f"Modifier not found: {p['name']}")
    obj.modifiers.remove(mod)
    return {"removed": p["name"]}


def _constraint_add(p: dict[str, Any]) -> dict[str, Any]:
    obj = bpy.data.objects.get(p["object"])
    if obj is None:
        raise BridgeError("OBJECT_NOT_FOUND", f"Object not found: {p['object']}")
    c = obj.constraints.new(p["type"])
    c.name = p["name"]
    changed = _apply_properties(c, p.get("properties") or {})
    return {"object": obj.name, "constraint": c.name, "type": c.type, "changed": changed}


def _constraint_remove(p: dict[str, Any]) -> dict[str, Any]:
    obj = bpy.data.objects.get(p["object"])
    c = obj.constraints.get(p["name"]) if obj else None
    if c is None:
        raise BridgeError("CONSTRAINT_NOT_FOUND", f"Constraint not found: {p['name']}")
    obj.constraints.remove(c)
    return {"removed": p["name"]}


def _scene_compositor_tree(scene: Any):
    if scene is None:
        raise BridgeError("SCENE_NOT_FOUND", "Scene was not found for compositor access.")

    # Blender 5.0 removed Scene.node_tree and made compositor trees reusable
    # node-group datablocks exposed through Scene.compositing_node_group.
    if hasattr(scene, "compositing_node_group"):
        tree = scene.compositing_node_group
        if tree is None:
            tree = bpy.data.node_groups.new(f"{scene.name} Compositor", "CompositorNodeTree")
            scene.compositing_node_group = tree
        return tree

    # Blender 4.5-and-earlier compatibility path.
    if hasattr(scene, "use_nodes"):
        scene.use_nodes = True
    tree = getattr(scene, "node_tree", None)
    if tree is None:
        raise BridgeError("COMPOSITOR_TREE_UNAVAILABLE", "Scene compositor node tree is unavailable.")
    return tree


def _ensure_compositor_output_image_socket(tree: Any) -> None:
    interface = getattr(tree, "interface", None)
    if interface is None:
        raise BridgeError("NODE_INTERFACE_UNAVAILABLE", "Compositor node tree has no interface API.")
    for item in list(interface.items_tree):
        if (
            getattr(item, "item_type", "") == "SOCKET"
            and getattr(item, "name", "") == "Image"
            and getattr(item, "in_out", "") == "OUTPUT"
        ):
            return
    interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")


def _effective_node_type(tree: Any, requested_type: str) -> str:
    if getattr(tree, "bl_idname", "") != "CompositorNodeTree" or bpy.app.version < (5, 0, 0):
        return requested_type
    if requested_type == "CompositorNodeComposite":
        _ensure_compositor_output_image_socket(tree)
        return "NodeGroupOutput"
    if requested_type == "CompositorNodeMixRGB":
        return "ShaderNodeMixRGB"
    return requested_type


def _node_tree(p: dict[str, Any]):
    kind = p["tree_type"]
    owner = p.get("owner") or ""
    if kind == "material":
        mat = bpy.data.materials.get(owner)
        if mat is None:
            raise BridgeError("MATERIAL_NOT_FOUND", f"Material not found: {owner}")
        mat.use_nodes = True
        return mat.node_tree
    if kind == "world":
        world = bpy.data.worlds.get(owner) if owner else bpy.context.scene.world
        if world is None:
            raise BridgeError("WORLD_NOT_FOUND", f"World not found: {owner}")
        world.use_nodes = True
        return world.node_tree
    if kind == "compositor":
        scene = bpy.data.scenes.get(owner) if owner else bpy.context.scene
        return _scene_compositor_tree(scene)
    if kind == "node_group":
        ng = bpy.data.node_groups.get(owner)
        if ng is None:
            raise BridgeError("NODE_GROUP_NOT_FOUND", f"Node group not found: {owner}")
        return ng
    if kind == "geometry_nodes":
        obj = bpy.data.objects.get(owner)
        mod = obj.modifiers.get(p.get("modifier") or "") if obj else None
        if mod is None or mod.type != "NODES":
            raise BridgeError("GEOMETRY_NODES_MODIFIER_NOT_FOUND", "Geometry Nodes modifier was not found.")
        if mod.node_group is None:
            raise BridgeError("NODE_GROUP_NOT_FOUND", "Geometry Nodes modifier has no node group.")
        return mod.node_group
    raise BridgeError("BAD_NODE_TREE", f"Unsupported node tree type: {kind}")


def _node_create(p: dict[str, Any]) -> dict[str, Any]:
    tree = _node_tree(p)
    if tree.nodes.get(p["name"]):
        raise BridgeError("NODE_EXISTS", f"Node already exists: {p['name']}")
    requested_type = p["node_type"]
    effective_type = _effective_node_type(tree, requested_type)
    node = tree.nodes.new(effective_type)
    node.name = p["name"]
    if p.get("label"):
        node.label = p["label"]
    if "location" in p:
        node.location = p["location"]
    result = {"name": node.name, "type": node.bl_idname}
    if effective_type != requested_type:
        result["requested_type"] = requested_type
        result["compatibility_alias"] = effective_type
    return result


def _node_remove(p: dict[str, Any]) -> dict[str, Any]:
    tree = _node_tree(p)
    node = tree.nodes.get(p["name"])
    if node is None:
        raise BridgeError("NODE_NOT_FOUND", f"Node not found: {p['name']}")
    tree.nodes.remove(node)
    return {"removed": p["name"]}


def _socket(sockets: Any, key: str):
    sock = sockets.get(key)
    if sock is not None:
        return sock
    if str(key).isdigit():
        idx = int(key)
        if 0 <= idx < len(sockets):
            return sockets[idx]
    raise BridgeError("SOCKET_NOT_FOUND", f"Socket not found: {key}")


def _node_set(p: dict[str, Any]) -> dict[str, Any]:
    tree = _node_tree(p)
    node = tree.nodes.get(p["name"])
    if node is None:
        raise BridgeError("NODE_NOT_FOUND", f"Node not found: {p['name']}")
    changed = _apply_properties(node, p.get("properties") or {})
    inputs = {}
    for key, value in (p.get("inputs") or {}).items():
        sock = _socket(node.inputs, key)
        if not hasattr(sock, "default_value"):
            raise BridgeError("SOCKET_READ_ONLY", f"Socket has no default value: {key}")
        sock.default_value = _resolve_ref(value)
        inputs[key] = _safe(sock.default_value)
    return {"node": node.name, "changed": changed, "inputs": inputs}


def _node_link(p: dict[str, Any]) -> dict[str, Any]:
    tree = _node_tree(p)
    src = tree.nodes.get(p["from_node"])
    dst = tree.nodes.get(p["to_node"])
    if src is None or dst is None:
        raise BridgeError("NODE_NOT_FOUND", "Source or destination node was not found.")
    out_socket = _socket(src.outputs, p["from_socket"])
    in_socket = _socket(dst.inputs, p["to_socket"])
    link = tree.links.new(out_socket, in_socket)
    return {"from_node": link.from_node.name, "to_node": link.to_node.name}


def _node_unlink(p: dict[str, Any]) -> dict[str, Any]:
    tree = _node_tree(p)
    removed = 0
    for link in list(tree.links):
        if p.get("clear_all") or (
            (not p.get("from_node") or link.from_node.name == p.get("from_node")) and
            (not p.get("to_node") or link.to_node.name == p.get("to_node"))
        ):
            tree.links.remove(link)
            removed += 1
    return {"removed_links": removed}


def _node_interface_socket(p: dict[str, Any]) -> dict[str, Any]:
    ng = bpy.data.node_groups.get(p["node_group"])
    if ng is None:
        raise BridgeError("NODE_GROUP_NOT_FOUND", f"Node group not found: {p['node_group']}")
    interface = getattr(ng, "interface", None)
    if interface is None:
        raise BridgeError("NODE_INTERFACE_UNAVAILABLE", "This Blender version does not expose node_group.interface.")
    if p["operation"] == "add":
        item = interface.new_socket(name=p["name"], in_out=p.get("in_out", "INPUT"), socket_type=p.get("socket_type") or "NodeSocketFloat")
        return {"added": item.name, "in_out": item.in_out, "socket_type": item.bl_socket_idname}
    for item in list(interface.items_tree):
        if getattr(item, "item_type", "") == "SOCKET" and item.name == p["name"] and getattr(item, "in_out", "") == p.get("in_out", "INPUT"):
            interface.remove(item)
            return {"removed": p["name"]}
    raise BridgeError("SOCKET_NOT_FOUND", f"Interface socket not found: {p['name']}")


def _keyframe(p: dict[str, Any], *, insert: bool) -> dict[str, Any]:
    obj = bpy.data.objects.get(p["object"])
    if obj is None:
        raise BridgeError("OBJECT_NOT_FOUND", f"Object not found: {p['object']}")
    kwargs = {"data_path": p["data_path"], "frame": float(p["frame"])}
    index = int(p.get("index", -1))
    if index >= 0:
        kwargs["index"] = index
    if insert and p.get("group"):
        kwargs["group"] = p["group"]
    ok = obj.keyframe_insert(**kwargs) if insert else obj.keyframe_delete(**kwargs)
    return {"object": obj.name, "data_path": p["data_path"], "frame": p["frame"], "success": bool(ok), "operation": "insert" if insert else "delete"}


def _operator_call(p: dict[str, Any]) -> dict[str, Any]:
    op = p["operator"]
    if "." not in op:
        raise BridgeError("OPERATOR_DENIED", "Operator must use category.operator syntax.")
    prefix, name = op.split(".", 1)
    if prefix not in SAFE_OPERATOR_PREFIXES or any(token in name for token in DANGEROUS_OPERATOR_TOKENS):
        raise BridgeError("OPERATOR_DENIED", f"Operator is outside the safe surface: {op}")
    props = p.get("properties") or {}
    for key in props:
        low = str(key).lower()
        if low in PATHLIKE_KEYS or any(token in low for token in ("filepath", "directory", "filename", "url", "script", "command")):
            raise BridgeError("OPERATOR_PATH_DENIED", f"Path/code-like operator property is denied: {key}")
    category = getattr(bpy.ops, prefix, None)
    operator = getattr(category, name, None) if category else None
    if operator is None:
        raise BridgeError("OPERATOR_NOT_FOUND", f"Operator not found: {op}")
    result = operator(**props)
    return {"operator": op, "result": sorted(result) if isinstance(result, set) else _safe(result)}


def _open_blend(root: Path, p: dict[str, Any]) -> dict[str, Any]:
    path = _workspace_path(root, p["path"], must_exist=True)
    if path.suffix.lower() != ".blend":
        raise BridgeError("BAD_BLEND_PATH", "open-blend accepts only .blend files.")
    bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=bool(p.get("load_ui", False)))
    return {"current_file": bpy.data.filepath}


def _save_blend(root: Path, p: dict[str, Any]) -> dict[str, Any]:
    path = _workspace_path(root, p["path"], must_exist=False)
    if path.suffix.lower() != ".blend":
        raise BridgeError("BAD_BLEND_PATH", "save-blend requires a .blend destination.")
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=bool(p.get("copy", False)), compress=bool(p.get("compress", True)))
    return {"current_file": bpy.data.filepath, "workspace_artifact": path.relative_to(root).as_posix()}


def _import_asset(root: Path, p: dict[str, Any]) -> dict[str, Any]:
    path = _workspace_path(root, p["path"], must_exist=True)
    fmt = p["format"]
    before = set(bpy.data.objects.keys())
    if fmt == "OBJ":
        result = bpy.ops.wm.obj_import(filepath=str(path))
    elif fmt == "FBX":
        result = bpy.ops.import_scene.fbx(filepath=str(path))
    elif fmt == "GLTF":
        result = bpy.ops.import_scene.gltf(filepath=str(path))
    elif fmt == "STL":
        result = bpy.ops.wm.stl_import(filepath=str(path))
    elif fmt == "PLY":
        result = bpy.ops.wm.ply_import(filepath=str(path))
    elif fmt == "USD":
        result = bpy.ops.wm.usd_import(filepath=str(path))
    elif fmt == "ABC":
        result = bpy.ops.wm.alembic_import(filepath=str(path))
    else:
        raise BridgeError("BAD_IMPORT_FORMAT", f"Unsupported import format: {fmt}")
    created = sorted(set(bpy.data.objects.keys()) - before)
    return {"result": sorted(result), "created_objects": created[:512]}


def _export_asset(root: Path, p: dict[str, Any]) -> dict[str, Any]:
    path = _workspace_path(root, p["path"], must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = p["format"]
    selected = bool(p.get("selection_only", False))
    if fmt == "OBJ":
        result = bpy.ops.wm.obj_export(filepath=str(path), export_selected_objects=selected)
    elif fmt == "FBX":
        result = bpy.ops.export_scene.fbx(filepath=str(path), use_selection=selected)
    elif fmt == "GLTF":
        result = bpy.ops.export_scene.gltf(filepath=str(path), use_selection=selected)
    elif fmt == "STL":
        result = bpy.ops.wm.stl_export(filepath=str(path), export_selected_objects=selected)
    elif fmt == "PLY":
        result = bpy.ops.wm.ply_export(filepath=str(path), export_selected_objects=selected)
    elif fmt == "USD":
        result = bpy.ops.wm.usd_export(filepath=str(path), selected_objects_only=selected)
    elif fmt == "ABC":
        result = bpy.ops.wm.alembic_export(filepath=str(path), selected=selected)
    else:
        raise BridgeError("BAD_EXPORT_FORMAT", f"Unsupported export format: {fmt}")
    return {"result": sorted(result), "workspace_artifact": path.relative_to(root).as_posix()}


def _render_still(root: Path, p: dict[str, Any]) -> dict[str, Any]:
    path = _workspace_path(root, p["path"], must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    old_path = scene.render.filepath
    try:
        scene.render.filepath = str(path)
        if "frame" in p:
            scene.frame_set(int(p["frame"]))
        bpy.ops.render.render(write_still=bool(p.get("write_still", True)))
    finally:
        scene.render.filepath = old_path
    artifact = path
    if not artifact.exists() and scene.render.use_file_extension:
        ext = "." + scene.render.image_settings.file_format.lower().replace("jpeg", "jpg").replace("open_exr", "exr")
        candidate = Path(str(path) + ext)
        if candidate.exists():
            artifact = candidate
    return {"workspace_artifact": artifact.relative_to(root).as_posix(), "frame": scene.frame_current}


def _render_animation(root: Path, p: dict[str, Any]) -> dict[str, Any]:
    out_dir = _workspace_path(root, p["output_dir"], must_exist=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    old = (scene.render.filepath, scene.frame_start, scene.frame_end, scene.render.image_settings.file_format)
    try:
        scene.render.filepath = str(out_dir / (p.get("filename_prefix") or "frame_"))
        if "frame_start" in p:
            scene.frame_start = int(p["frame_start"])
        if "frame_end" in p:
            scene.frame_end = int(p["frame_end"])
        scene.render.image_settings.file_format = p.get("file_format", "PNG")
        bpy.ops.render.render(animation=True)
    finally:
        scene.render.filepath, scene.frame_start, scene.frame_end, scene.render.image_settings.file_format = old
    files = sorted(x for x in out_dir.iterdir() if x.is_file())[:64]
    return {"workspace_artifacts": [x.relative_to(root).as_posix() for x in files], "file_count_returned": len(files)}


class FB_PT_bridge_panel(bpy.types.Panel):
    bl_label = "FolderBridge Blender"
    bl_idname = "FB_PT_blender_bridge"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "FolderBridge"

    def draw(self, context):
        layout = self.layout
        layout.label(text=f"Bridge: 127.0.0.1:{PORT}")
        layout.label(text="Status: Online" if _SERVER is not None else "Status: Offline")
        comfy = "comfyui_blender" in context.preferences.addons
        layout.label(text=f"ComfyUI Add-on: {'Enabled' if comfy else 'Disabled'}")


_CLASSES = (FB_PT_bridge_panel,)


def _start_server() -> None:
    global _SERVER, _SERVER_THREAD, _TIMER_REGISTERED
    _token()
    if _SERVER is not None:
        return
    try:
        _SERVER = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    except OSError as exc:
        raise RuntimeError(f"FolderBridge Blender Bridge could not bind {HOST}:{PORT}: {exc}") from exc
    _SERVER.daemon_threads = True
    _SERVER_THREAD = threading.Thread(target=_SERVER.serve_forever, name="FolderBridgeBlenderBridge", daemon=True)
    _SERVER_THREAD.start()
    if not _TIMER_REGISTERED:
        bpy.app.timers.register(_drain_requests, first_interval=0.05, persistent=True)
        _TIMER_REGISTERED = True


def _stop_server() -> None:
    global _SERVER, _SERVER_THREAD, _TIMER_REGISTERED
    if _SERVER is not None:
        try:
            _SERVER.shutdown()
            _SERVER.server_close()
        except Exception:
            pass
    _SERVER = None
    _SERVER_THREAD = None
    if _TIMER_REGISTERED:
        try:
            bpy.app.timers.unregister(_drain_requests)
        except Exception:
            pass
        _TIMER_REGISTERED = False


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    _start_server()
    print(f"[FolderBridge Blender Bridge] listening on http://{HOST}:{PORT}")


def unregister():
    _stop_server()
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

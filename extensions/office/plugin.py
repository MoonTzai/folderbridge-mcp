from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from folderbridge_mcp.process_control import owned_process_group_kwargs, terminate_owned_process_tree


MAX_OFFICE_BYTES = 512 * 1024 * 1024
MAX_ZIP_MEMBERS = 20_000
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_RENDER_FILES = 10_000
DENIED_PARTS = {
    ".git", ".svn", ".hg", ".venv", "venv", "node_modules", "__pycache__",
    "build", "dist", "target", "vendor", ".idea", ".vscode",
}

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
DCTERMS = "http://purl.org/dc/terms/"
NS_W = {"w": W, "r": R, "pr": PR}
NS_S = {"s": S, "r": R, "pr": PR}
CELL_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)$")
RANGE_RE = re.compile(r"^\s*\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)(?::\$?([A-Za-z]{1,3})\$?([1-9][0-9]*))?\s*$")


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if action == "status":
        return _status()
    workspace_root = _workspace_root(context)
    if action == "inspect_docx":
        path = _resolve_input(workspace_root, params["path"], {".docx"})
        return _inspect_docx(path, workspace_root, int(params.get("max_items", 2000)))
    if action == "inspect_xlsx":
        path = _resolve_input(workspace_root, params["path"], {".xlsx"})
        return _inspect_xlsx(
            path,
            workspace_root,
            sheet=params.get("sheet"),
            cell_range=params.get("cell_range"),
            max_items=int(params.get("max_items", 5000)),
            include_empty=bool(params.get("include_empty", False)),
        )
    if action == "render":
        if bool(context.get("workspace_read_only")):
            raise RuntimeError("FolderBridge is read-only; native Office rendering writes output files.")
        return _render(params, context, workspace_root)
    raise RuntimeError(f"unsupported action: {action}")


def _status() -> dict[str, Any]:
    result: dict[str, Any] = {
        "platform": sys.platform,
        "windows": sys.platform == "win32",
        "powershell": shutil.which("powershell.exe") if sys.platform == "win32" else None,
        "windows_pdf_renderer": sys.platform == "win32",
        "office": {"powerpoint": False, "word": False, "excel": False},
        "native_render_ready": False,
    }
    if sys.platform != "win32":
        result["reason"] = "Native Office rendering requires Windows and locally installed Microsoft Office. OOXML inspection remains portable."
        return result
    try:
        import winreg

        for key, progid in (("powerpoint", "PowerPoint.Application"), ("word", "Word.Application"), ("excel", "Excel.Application")):
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\CLSID") as handle:
                    value, _kind = winreg.QueryValueEx(handle, None)
                result["office"][key] = bool(value)
            except OSError:
                result["office"][key] = False
    except Exception as exc:
        result["registry_error"] = f"{type(exc).__name__}: {exc}"
    result["native_render_ready"] = bool(result["powershell"] and any(result["office"].values()))
    return result


def _workspace_root(context: dict[str, Any]) -> Path:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("workspace_root is required")
    root = Path(raw).resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("workspace_root is not a directory")
    return root


def _is_reparse(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def _clean_relative(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise RuntimeError("paths must be non-empty POSIX-style workspace-relative strings")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise RuntimeError("path must stay inside the selected workspace")
    if any(part.lower() in DENIED_PARTS for part in rel.parts):
        raise RuntimeError("path targets a denied generated/dependency/VCS directory")
    return rel


def _resolve_input(root: Path, raw: str, extensions: set[str]) -> Path:
    rel = _clean_relative(raw)
    candidate = root.joinpath(*rel.parts)
    _reject_links(root, candidate)
    try:
        path = candidate.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("input path escapes the selected workspace") from exc
    if not path.is_file() or path.is_symlink() or _is_reparse(path):
        raise RuntimeError("input must be a regular non-link file")
    if path.suffix.lower() not in extensions:
        raise RuntimeError(f"unsupported file type: {path.suffix.lower() or '<none>'}")
    size = path.stat().st_size
    if size > MAX_OFFICE_BYTES:
        raise RuntimeError(f"Office file exceeds {MAX_OFFICE_BYTES} bytes")
    return path


def _resolve_output_dir(root: Path, raw: str) -> Path:
    rel = _clean_relative(raw)
    candidate = root.joinpath(*rel.parts)
    _reject_links(root, candidate)
    try:
        parent = candidate.parent.resolve(strict=True)
        parent.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("output directory parent must already exist inside the workspace") from exc
    candidate.mkdir(exist_ok=True)
    _reject_links(root, candidate)
    resolved = candidate.resolve(strict=True)
    resolved.relative_to(root)
    if not resolved.is_dir() or resolved.is_symlink() or _is_reparse(resolved):
        raise RuntimeError("output_dir must be a regular directory")
    return resolved


def _reject_links(root: Path, candidate: Path) -> None:
    try:
        parts = candidate.relative_to(root).parts
    except ValueError as exc:
        raise RuntimeError("path escapes workspace") from exc
    current = root
    for part in parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        if current.is_symlink() or _is_reparse(current):
            raise RuntimeError(f"linked/reparse path component is not allowed: {part}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_ooxml(path: Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError("file is not a readable OOXML ZIP package") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        archive.close()
        raise RuntimeError(f"OOXML package exceeds {MAX_ZIP_MEMBERS} members")
    total = sum(info.file_size for info in infos)
    if total > MAX_UNCOMPRESSED_BYTES:
        archive.close()
        raise RuntimeError(f"OOXML package exceeds {MAX_UNCOMPRESSED_BYTES} uncompressed bytes")
    return archive


def _read_xml(archive: zipfile.ZipFile, name: str, *, required: bool = True) -> ET.Element | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        if required:
            raise RuntimeError(f"required OOXML part is missing: {name}")
        return None
    if info.file_size > MAX_XML_BYTES:
        raise RuntimeError(f"OOXML XML part is too large: {name}")
    try:
        return ET.fromstring(archive.read(info))
    except ET.ParseError as exc:
        raise RuntimeError(f"invalid OOXML XML part: {name}") from exc


def _word_text(node: ET.Element) -> str:
    pieces: list[str] = []
    for child in node.iter():
        local = child.tag.rsplit("}", 1)[-1]
        if local == "t":
            pieces.append(child.text or "")
        elif local == "tab":
            pieces.append("\t")
        elif local in {"br", "cr"}:
            pieces.append("\n")
    return "".join(pieces).replace("\r", "")


def _word_relations(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    root = _read_xml(archive, name, required=False)
    if root is None:
        return []
    out: list[dict[str, str]] = []
    for rel in root.findall(f"{{{PR}}}Relationship"):
        out.append({
            "id": rel.attrib.get("Id", ""),
            "type": rel.attrib.get("Type", ""),
            "target": rel.attrib.get("Target", ""),
            "target_mode": rel.attrib.get("TargetMode", ""),
        })
    return out


def _core_properties(archive: zipfile.ZipFile) -> dict[str, str]:
    root = _read_xml(archive, "docProps/core.xml", required=False)
    if root is None:
        return {}
    keys = {
        "title": f"{{{DC}}}title",
        "subject": f"{{{DC}}}subject",
        "creator": f"{{{DC}}}creator",
        "description": f"{{{DC}}}description",
        "created": f"{{{DCTERMS}}}created",
        "modified": f"{{{DCTERMS}}}modified",
    }
    result: dict[str, str] = {}
    for key, tag in keys.items():
        node = root.find(tag)
        if node is not None and node.text:
            result[key] = node.text
    return result


def _inspect_docx(path: Path, root: Path, max_items: int) -> dict[str, Any]:
    with _open_ooxml(path) as archive:
        names = set(archive.namelist())
        document = _read_xml(archive, "word/document.xml")
        assert document is not None
        paragraphs: list[dict[str, Any]] = []
        all_paragraphs = document.findall(".//w:p", NS_W)
        for index, paragraph in enumerate(all_paragraphs[:max_items], start=1):
            ppr = paragraph.find("w:pPr", NS_W)
            style = None
            num_id = None
            level = None
            if ppr is not None:
                style_node = ppr.find("w:pStyle", NS_W)
                if style_node is not None:
                    style = style_node.attrib.get(f"{{{W}}}val")
                numpr = ppr.find("w:numPr", NS_W)
                if numpr is not None:
                    num_node = numpr.find("w:numId", NS_W)
                    ilvl_node = numpr.find("w:ilvl", NS_W)
                    if num_node is not None:
                        num_id = num_node.attrib.get(f"{{{W}}}val")
                    if ilvl_node is not None:
                        level = ilvl_node.attrib.get(f"{{{W}}}val")
            text = _word_text(paragraph)
            paragraphs.append({"index": index, "style": style, "num_id": num_id, "level": level, "text": text})

        tables: list[dict[str, Any]] = []
        all_tables = document.findall(".//w:tbl", NS_W)
        for table_index, table in enumerate(all_tables[: min(max_items, 1000)], start=1):
            rows: list[list[str]] = []
            for row in table.findall("w:tr", NS_W):
                rows.append([_word_text(cell).strip() for cell in row.findall("w:tc", NS_W)])
            tables.append({"index": table_index, "rows": rows})

        sections: list[dict[str, Any]] = []
        for index, sect in enumerate(document.findall(".//w:sectPr", NS_W), start=1):
            pg_sz = sect.find("w:pgSz", NS_W)
            pg_mar = sect.find("w:pgMar", NS_W)
            sections.append({
                "index": index,
                "page_size": dict(pg_sz.attrib) if pg_sz is not None else {},
                "margins": dict(pg_mar.attrib) if pg_mar is not None else {},
            })

        relations = _word_relations(archive, "word/_rels/document.xml.rels")
        hyperlinks = [rel for rel in relations if rel["type"].endswith("/hyperlink")]
        media = []
        for name in sorted(n for n in names if n.startswith("word/media/") and not n.endswith("/")):
            info = archive.getinfo(name)
            media.append({"part": name, "size": info.file_size})

        stories: dict[str, list[dict[str, Any]]] = {}
        for prefix in ("header", "footer"):
            items: list[dict[str, Any]] = []
            for name in sorted(n for n in names if re.fullmatch(rf"word/{prefix}[0-9]+\.xml", n)):
                story = _read_xml(archive, name)
                assert story is not None
                items.append({"part": name, "text": _word_text(story)[:20000]})
            stories[prefix + "s"] = items

        annotations: dict[str, Any] = {}
        for kind, filename, tag in (
            ("footnotes", "word/footnotes.xml", "footnote"),
            ("endnotes", "word/endnotes.xml", "endnote"),
            ("comments", "word/comments.xml", "comment"),
        ):
            ann_root = _read_xml(archive, filename, required=False)
            if ann_root is None:
                annotations[kind] = {"count": 0, "items": []}
                continue
            nodes = ann_root.findall(f"w:{tag}", NS_W)
            annotations[kind] = {
                "count": len(nodes),
                "items": [
                    {"id": node.attrib.get(f"{{{W}}}id"), "text": _word_text(node)[:20000]}
                    for node in nodes[: min(max_items, 1000)]
                ],
            }

        styles_root = _read_xml(archive, "word/styles.xml", required=False)
        style_count = len(styles_root.findall("w:style", NS_W)) if styles_root is not None else 0

        return {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
            "format": "docx",
            "core_properties": _core_properties(archive),
            "paragraph_count": len(all_paragraphs),
            "paragraphs_truncated": len(all_paragraphs) > max_items,
            "paragraphs": paragraphs,
            "table_count": len(all_tables),
            "tables_truncated": len(all_tables) > min(max_items, 1000),
            "tables": tables,
            "section_count": len(sections),
            "sections": sections,
            "style_count": style_count,
            "media_count": len(media),
            "media": media[:max_items],
            "hyperlinks": hyperlinks[:max_items],
            **stories,
            **annotations,
        }


def _normalize_package_target(base: str, target: str) -> str:
    target = target.replace("\\", "/")
    if target.startswith("/"):
        normalized = posixpath.normpath(target.lstrip("/"))
    else:
        normalized = posixpath.normpath(posixpath.join(base, target))
    if normalized == ".." or normalized.startswith("../"):
        raise RuntimeError("OOXML relationship target escapes package root")
    return normalized


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    root = _read_xml(archive, "xl/sharedStrings.xml", required=False)
    if root is None:
        return []
    values: list[str] = []
    for item in root.findall("s:si", NS_S):
        values.append("".join(node.text or "" for node in item.findall(".//s:t", NS_S)))
    return values


def _col_number(value: str) -> int:
    result = 0
    for char in value.upper():
        result = result * 26 + (ord(char) - 64)
    return result


def _parse_range(raw: str | None) -> tuple[int, int, int, int] | None:
    if raw is None:
        return None
    match = RANGE_RE.fullmatch(raw)
    if not match:
        raise RuntimeError("cell_range must be A1 or A1:B20")
    c1, r1, c2, r2 = match.groups()
    c2 = c2 or c1
    r2 = r2 or r1
    left, right = sorted((_col_number(c1), _col_number(c2)))
    top, bottom = sorted((int(r1), int(r2)))
    return left, top, right, bottom


def _cell_in_range(address: str, bounds: tuple[int, int, int, int] | None) -> bool:
    if bounds is None:
        return True
    match = CELL_RE.fullmatch(address)
    if not match:
        return False
    col, row = match.groups()
    col_num = _col_number(col)
    row_num = int(row)
    left, top, right, bottom = bounds
    return left <= col_num <= right and top <= row_num <= bottom


def _xlsx_value(cell: ET.Element, shared: list[str]) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//s:t", NS_S))
    value_node = cell.find("s:v", NS_S)
    raw = value_node.text if value_node is not None else None
    if raw is None:
        return None
    if cell_type == "s":
        try:
            index = int(raw)
            return shared[index] if 0 <= index < len(shared) else raw
        except ValueError:
            return raw
    if cell_type == "b":
        return raw == "1"
    if cell_type in {"str", "e", "d"}:
        return raw
    try:
        if any(char in raw.lower() for char in (".", "e")):
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _inspect_xlsx(
    path: Path,
    root: Path,
    *,
    sheet: str | None,
    cell_range: str | None,
    max_items: int,
    include_empty: bool,
) -> dict[str, Any]:
    bounds = _parse_range(cell_range)
    with _open_ooxml(path) as archive:
        names = set(archive.namelist())
        workbook = _read_xml(archive, "xl/workbook.xml")
        rels = _read_xml(archive, "xl/_rels/workbook.xml.rels")
        assert workbook is not None and rels is not None
        rel_map: dict[str, str] = {}
        for rel in rels.findall(f"{{{PR}}}Relationship"):
            rid = rel.attrib.get("Id")
            target = rel.attrib.get("Target")
            if rid and target:
                rel_map[rid] = _normalize_package_target("xl", target)
        shared = _shared_strings(archive)
        sheets_meta: list[dict[str, Any]] = []
        for index, node in enumerate(workbook.findall("s:sheets/s:sheet", NS_S), start=1):
            name = node.attrib.get("name", f"Sheet{index}")
            rid = node.attrib.get(f"{{{R}}}id", "")
            sheets_meta.append({
                "index": index,
                "name": name,
                "state": node.attrib.get("state", "visible"),
                "relationship_id": rid,
                "part": rel_map.get(rid),
            })
        if sheet is not None and not any(item["name"] == sheet for item in sheets_meta):
            raise RuntimeError(f"worksheet not found: {sheet}")

        rendered_sheets: list[dict[str, Any]] = []
        remaining = max_items
        for meta in sheets_meta:
            if sheet is not None and meta["name"] != sheet:
                continue
            part = meta.get("part")
            if not isinstance(part, str) or part not in names:
                rendered_sheets.append({**meta, "error": "worksheet XML part missing"})
                continue
            sheet_root = _read_xml(archive, part)
            assert sheet_root is not None
            dimension = sheet_root.find("s:dimension", NS_S)
            merges = [node.attrib.get("ref", "") for node in sheet_root.findall("s:mergeCells/s:mergeCell", NS_S)]
            hidden_rows: list[int] = []
            rows = sheet_root.findall("s:sheetData/s:row", NS_S)
            cells: list[dict[str, Any]] = []
            formula_count = 0
            nonempty_count = 0
            for row_node in rows:
                if row_node.attrib.get("hidden") in {"1", "true", "True"}:
                    try:
                        hidden_rows.append(int(row_node.attrib.get("r", "0")))
                    except ValueError:
                        pass
                for cell in row_node.findall("s:c", NS_S):
                    address = cell.attrib.get("r", "")
                    if not _cell_in_range(address, bounds):
                        continue
                    formula_node = cell.find("s:f", NS_S)
                    formula = formula_node.text if formula_node is not None else None
                    if formula is not None:
                        formula_count += 1
                    value = _xlsx_value(cell, shared)
                    if value not in (None, "") or formula:
                        nonempty_count += 1
                    if not include_empty and value in (None, "") and not formula:
                        continue
                    if remaining <= 0:
                        continue
                    cells.append({
                        "address": address,
                        "type": cell.attrib.get("t"),
                        "style": cell.attrib.get("s"),
                        "formula": formula,
                        "value": value,
                    })
                    remaining -= 1
            cols: list[dict[str, Any]] = []
            for col in sheet_root.findall("s:cols/s:col", NS_S):
                cols.append({
                    "min": col.attrib.get("min"),
                    "max": col.attrib.get("max"),
                    "width": col.attrib.get("width"),
                    "hidden": col.attrib.get("hidden") in {"1", "true", "True"},
                })
            rendered_sheets.append({
                **meta,
                "dimension": dimension.attrib.get("ref") if dimension is not None else None,
                "row_count_xml": len(rows),
                "nonempty_cell_count_in_selected_range": nonempty_count,
                "formula_count_in_selected_range": formula_count,
                "cells": cells,
                "cells_truncated": remaining <= 0,
                "merged_ranges": merges[:max_items],
                "hidden_rows": hidden_rows[:max_items],
                "columns": cols[:max_items],
            })

        defined_names = []
        for node in workbook.findall("s:definedNames/s:definedName", NS_S):
            defined_names.append({
                "name": node.attrib.get("name", ""),
                "local_sheet_id": node.attrib.get("localSheetId"),
                "value": node.text or "",
            })
        external_parts = sorted(name for name in names if name.startswith("xl/externalLinks/") and name.endswith(".xml"))
        calc = workbook.find("s:calcPr", NS_S)
        return {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
            "format": "xlsx",
            "core_properties": _core_properties(archive),
            "sheet_count": len(sheets_meta),
            "sheets": rendered_sheets,
            "all_sheets": sheets_meta,
            "shared_string_count": len(shared),
            "defined_names": defined_names[:max_items],
            "external_link_parts": external_parts[:max_items],
            "calculation": dict(calc.attrib) if calc is not None else {},
            "selected_sheet": sheet,
            "selected_cell_range": cell_range,
            "max_items": max_items,
        }


def _render(params: dict[str, Any], context: dict[str, Any], root: Path) -> dict[str, Any]:
    path = _resolve_input(root, params["path"], {".pptx", ".docx", ".xlsx"})
    output_dir = _resolve_output_dir(root, params["output_dir"])
    overwrite = bool(params.get("overwrite", False))
    if not overwrite and any(output_dir.iterdir()):
        raise RuntimeError("output_dir is not empty; choose a fresh directory or set overwrite=true")
    if sys.platform != "win32":
        raise RuntimeError("native Microsoft Office rendering is available only on Windows")
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("powershell.exe was not found on the trusted PATH")
    state_dir_raw = context.get("state_dir")
    if not isinstance(state_dir_raw, str) or not state_dir_raw:
        raise RuntimeError("extension state_dir is unavailable")
    state_dir = Path(state_dir_raw).resolve(strict=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="office-render-", dir=state_dir))
    script = Path(__file__).resolve().with_name("office.ps1")
    if not script.is_file():
        raise RuntimeError("bundled Office renderer script is missing")

    page_start = params.get("page_start")
    page_end = params.get("page_end")
    if page_start is not None and page_end is not None and int(page_start) > int(page_end):
        raise RuntimeError("page_start must be <= page_end")
    width = int(params.get("width", 1920))
    sheets = params.get("sheets") or []
    argv = [
        str(powershell), "-NoProfile", "-NonInteractive", "-Sta", "-ExecutionPolicy", "Bypass",
        "-File", str(script),
        "-InputPath", str(path),
        "-OutputDir", str(output_dir),
        "-TempDir", str(temp_dir),
        "-Width", str(width),
        "-Overwrite", ("true" if overwrite else "false"),
    ]
    if page_start is not None:
        argv += ["-PageStart", str(int(page_start))]
    if page_end is not None:
        argv += ["-PageEnd", str(int(page_end))]
    if sheets:
        argv += ["-SheetsJson", json.dumps(sheets, ensure_ascii=False, separators=(",", ":"))]

    try:
        try:
            process = subprocess.Popen(
                argv,
                cwd=script.parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                **owned_process_group_kwargs(hide_window=True),
            )
        except OSError as exc:
            raise RuntimeError(f"could not start Microsoft Office renderer: {exc}") from exc
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=570)
        except subprocess.TimeoutExpired as exc:
            terminate_owned_process_tree(process, hide_window=True)
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout_bytes, stderr_bytes = process.communicate(timeout=5)
            raise RuntimeError("Microsoft Office rendering exceeded 570 seconds") from exc
        completed = subprocess.CompletedProcess(argv, process.returncode, stdout_bytes, stderr_bytes)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    stdout = completed.stdout.decode("utf-8-sig", errors="replace").strip()
    stderr = completed.stderr.decode("utf-8-sig", errors="replace").strip()
    if completed.returncode != 0:
        detail = stderr or stdout or f"exit code {completed.returncode}"
        raise RuntimeError(f"native Office render failed: {detail[:4000]}")
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"native Office renderer returned invalid JSON: {stdout[:1000]}") from exc
    if not isinstance(result, dict) or not isinstance(result.get("files"), list):
        raise RuntimeError("native Office renderer returned an invalid result envelope")

    generated: list[dict[str, Any]] = []
    for raw_name in result["files"]:
        if not isinstance(raw_name, str) or not raw_name or "\\" in raw_name:
            raise RuntimeError("renderer returned an invalid output filename")
        rel = PurePosixPath(raw_name)
        if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 1:
            raise RuntimeError("renderer output must stay directly inside output_dir")
        candidate = output_dir / rel.name
        _reject_links(root, candidate)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(output_dir)
        if not resolved.is_file() or resolved.suffix.lower() != ".png":
            raise RuntimeError("renderer output is not a regular PNG")
        generated.append({
            "path": resolved.relative_to(root).as_posix(),
            "size": resolved.stat().st_size,
            "sha256": _sha256(resolved),
        })
    if not generated:
        raise RuntimeError("native Office renderer produced no PNG files")
    if len(generated) > MAX_RENDER_FILES:
        raise RuntimeError(f"renderer produced more than {MAX_RENDER_FILES} files")

    archive_meta = None
    if bool(params.get("make_zip", True)):
        archive = output_dir.with_suffix(".zip") if output_dir.suffix else Path(str(output_dir) + ".zip")
        archive.relative_to(root)
        _reject_links(root, archive)
        if archive.exists() and not overwrite:
            raise RuntimeError("render ZIP already exists; choose a fresh output_dir or set overwrite=true")
        temporary = archive.with_name(f".{archive.name}.tmp")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for item in generated:
                    source = root / PurePosixPath(item["path"])
                    zf.write(source, arcname=source.name)
            os.replace(temporary, archive)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        archive_meta = {
            "path": archive.relative_to(root).as_posix(),
            "size": archive.stat().st_size,
            "sha256": _sha256(archive),
        }

    return {
        "path": path.relative_to(root).as_posix(),
        "source_size": path.stat().st_size,
        "source_sha256": _sha256(path),
        "format": path.suffix.lower().lstrip("."),
        "backend": result.get("backend"),
        "application": result.get("application"),
        "source_units": result.get("source_units"),
        "selected_range": result.get("selected_range"),
        "rendered_count": len(generated),
        "files": generated,
        "archive": archive_meta,
        "notes": result.get("notes", []),
    }

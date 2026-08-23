from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import posixpath
import re
import struct
import zipfile
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from .security import ToolError, Workspace

MAX_BINARY_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PPTX_BYTES = 512 * 1024 * 1024
MAX_ZIP_MEMBERS = 20_000
MAX_XML_MEMBER_BYTES = 16 * 1024 * 1024
MAX_PPTX_XML_TOTAL_BYTES = 256 * 1024 * 1024
MAX_SMARTART_ITEMS = 5_000
MAX_PPTX_PAGE_SPAN = 100
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_ARCHIVE_BYTES = 512 * 1024 * 1024

_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
_DATA_RE = re.compile(r"^ppt/diagrams/data(\d+)\.xml$")

_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_R_DM = f"{{{_NS['r']}}}dm"


def file_info(workspace: Workspace, raw: str) -> dict[str, Any]:
    path = workspace.resolve(raw)
    if not path.is_file():
        raise ToolError("NOT_FOUND", "File does not exist.", path=raw)
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise ToolError("READ_FAILED", f"Could not stat {raw}: {exc}") from exc
    if stat_result.st_size > MAX_BINARY_FILE_BYTES:
        raise ToolError(
            "FILE_TOO_LARGE",
            f"Binary metadata hashing is limited to {MAX_BINARY_FILE_BYTES} bytes.",
            path=raw,
            size=stat_result.st_size,
        )
    mime_type, encoding = mimetypes.guess_type(path.name)
    return {
        "path": path.relative_to(workspace.root).as_posix(),
        "size": stat_result.st_size,
        "sha256": _sha256_file(path),
        "mime_type": mime_type or "application/octet-stream",
        "content_encoding": encoding,
        "modified_ns": stat_result.st_mtime_ns,
        "is_file": True,
    }


def inspect_pptx(
    workspace: Workspace,
    raw: str,
    *,
    page_start: int | None = None,
    page_end: int | None = None,
) -> dict[str, Any]:
    path = workspace.resolve(raw)
    if not path.is_file():
        raise ToolError("NOT_FOUND", "File does not exist.", path=raw)
    size = path.stat().st_size
    if size > MAX_PPTX_BYTES:
        raise ToolError("FILE_TOO_LARGE", f"PPTX inspection is limited to {MAX_PPTX_BYTES} bytes.", path=raw, size=size)
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ToolError("INVALID_PPTX", "File is not a readable PPTX/ZIP package.", path=raw) from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise ToolError("ZIP_TOO_MANY_MEMBERS", f"Archive exceeds {MAX_ZIP_MEMBERS} members.", path=raw)
        xml_total = sum(
            info.file_size
            for info in infos
            if info.filename.lower().endswith((".xml", ".rels"))
        )
        if xml_total > MAX_PPTX_XML_TOTAL_BYTES:
            raise ToolError(
                "PPTX_XML_TOO_LARGE",
                f"PPTX XML relationships/content exceed {MAX_PPTX_XML_TOTAL_BYTES} uncompressed bytes.",
                path=raw,
                uncompressed_xml_bytes=xml_total,
            )
        names = {info.filename for info in infos}
        slide_numbers = sorted(
            int(match.group(1))
            for name in names
            if (match := _SLIDE_RE.match(name)) is not None
        )
        if not slide_numbers:
            raise ToolError("INVALID_PPTX", "No ppt/slides/slide*.xml parts were found.", path=raw)
        data_parts = sorted(
            (int(match.group(1)), name)
            for name in names
            if (match := _DATA_RE.match(name)) is not None
        )
        start, end = _page_range(page_start, page_end, slide_numbers)

        all_frames: list[dict[str, Any]] = []
        selected_frames: list[dict[str, Any]] = []
        selected_roots: dict[int, ET.Element] = {}
        anomalies: list[dict[str, Any]] = []

        for page in slide_numbers:
            slide_name = f"ppt/slides/slide{page}.xml"
            rels_name = f"ppt/slides/_rels/slide{page}.xml.rels"
            try:
                slide_root = _read_xml_member(archive, slide_name)
            except ToolError as exc:
                anomalies.append({"page": page, "kind": exc.code, "part": slide_name})
                continue
            if start <= page <= end:
                selected_roots[page] = slide_root

            rels_by_id: dict[str, dict[str, str]] = {}
            if rels_name in names:
                try:
                    rels_root = _read_xml_member(archive, rels_name)
                    for relationship in rels_root.findall("pr:Relationship", _NS):
                        rel_id = relationship.attrib.get("Id")
                        if rel_id:
                            rels_by_id[rel_id] = {
                                "type": relationship.attrib.get("Type", ""),
                                "target": relationship.attrib.get("Target", ""),
                                "target_mode": relationship.attrib.get("TargetMode", ""),
                            }
                except ToolError as exc:
                    anomalies.append({"page": page, "kind": exc.code, "part": rels_name})

            for frame_index, rel_ids_element in enumerate(slide_root.findall(".//dgm:relIds", _NS), start=1):
                dm_rid = rel_ids_element.attrib.get(_R_DM)
                mapping: dict[str, Any] = {
                    "page": page,
                    "frame_index": frame_index,
                    "relationship_id": dm_rid,
                    "data_part": None,
                    "mapping_ok": False,
                }
                relation = rels_by_id.get(dm_rid or "")
                if relation is None:
                    mapping["error"] = "diagram data relationship not found"
                    anomalies.append({
                        "page": page,
                        "frame_index": frame_index,
                        "kind": "MISSING_DIAGRAM_RELATIONSHIP",
                        "relationship_id": dm_rid,
                    })
                elif not relation["type"].endswith("/diagramData"):
                    mapping["error"] = "relationship is not diagramData"
                    anomalies.append({
                        "page": page,
                        "frame_index": frame_index,
                        "kind": "WRONG_DIAGRAM_RELATIONSHIP_TYPE",
                        "relationship_id": dm_rid,
                        "type": relation["type"],
                    })
                else:
                    target = _normalize_package_target("ppt/slides", relation["target"])
                    mapping["data_part"] = target
                    if target not in names:
                        mapping["error"] = "diagram data target missing from package"
                        anomalies.append({
                            "page": page,
                            "frame_index": frame_index,
                            "kind": "MISSING_DIAGRAM_DATA",
                            "data_part": target,
                        })
                    else:
                        mapping["mapping_ok"] = True
                all_frames.append(mapping)
                if start <= page <= end:
                    selected_frames.append(mapping)

        mapped_parts = [item["data_part"] for item in all_frames if item.get("mapping_ok") and item.get("data_part")]
        mapped_set = set(mapped_parts)
        data_names = [name for _, name in data_parts]
        orphan_parts = [name for name in data_names if name not in mapped_set]
        duplicates: dict[str, list[dict[str, int]]] = {}
        for data_part in sorted(mapped_set):
            occurrences = [
                {"page": item["page"], "frame_index": item["frame_index"]}
                for item in all_frames
                if item.get("data_part") == data_part
            ]
            if len(occurrences) > 1:
                duplicates[data_part] = occurrences

        selected_pages: list[dict[str, Any]] = []
        for page in range(start, end + 1):
            frames = [item for item in selected_frames if item["page"] == page]
            rendered_frames: list[dict[str, Any]] = []
            for frame in frames:
                rendered = dict(frame)
                data_part = frame.get("data_part")
                if frame.get("mapping_ok") and isinstance(data_part, str):
                    rendered["smartart"] = _parse_smartart(archive, data_part)
                rendered_frames.append(rendered)
            slide_root = selected_roots.get(page)
            selected_pages.append({
                "page": page,
                "slide_text": _slide_text(slide_root) if slide_root is not None else "",
                "smartart_frames": rendered_frames,
            })

        return {
            "path": path.relative_to(workspace.root).as_posix(),
            "size": size,
            "sha256": _sha256_file(path),
            "slides": len(slide_numbers),
            "slide_numbers": slide_numbers,
            "smartart_data_xml": len(data_parts),
            "smartart_frames": len(all_frames),
            "xml_uncompressed_bytes": xml_total,
            "xml_limit_bytes": MAX_PPTX_XML_TOTAL_BYTES,
            "xml_limit_usage_ratio": xml_total / MAX_PPTX_XML_TOTAL_BYTES,
            "successful_diagram_mappings": sum(1 for item in all_frames if item.get("mapping_ok")),
            "unique_mapped_data_parts": len(mapped_set),
            "orphan_data_parts": orphan_parts,
            "duplicate_data_targets": duplicates,
            "selected_range": {"page_start": start, "page_end": end},
            "selected_smartart_frames": len(selected_frames),
            "pages": selected_pages,
            "anomalies": anomalies,
        }


def open_image(
    workspace: Workspace,
    *,
    raw: str | None = None,
    archive_raw: str | None = None,
    member: str | None = None,
) -> dict[str, Any]:
    direct = isinstance(raw, str) and bool(raw)
    archived = isinstance(archive_raw, str) and bool(archive_raw)
    if direct == archived:
        raise ToolError("INVALID_ARGUMENT", "Provide exactly one of path or archive_path.")

    source_meta: dict[str, Any]
    if direct:
        assert isinstance(raw, str)
        path = workspace.resolve(raw)
        if not path.is_file():
            raise ToolError("NOT_FOUND", "Image file does not exist.", path=raw)
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise ToolError("IMAGE_TOO_LARGE", f"Image exceeds {MAX_IMAGE_BYTES} bytes.", path=raw, size=size)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ToolError("READ_FAILED", f"Could not read {raw}: {exc}") from exc
        source_meta = {
            "path": path.relative_to(workspace.root).as_posix(),
            "archive_path": None,
            "member": None,
        }
    else:
        assert isinstance(archive_raw, str)
        if not isinstance(member, str) or not member:
            raise ToolError("INVALID_ARGUMENT", "member is required with archive_path.")
        _validate_archive_member(member)
        archive_path = workspace.resolve(archive_raw)
        if not archive_path.is_file():
            raise ToolError("NOT_FOUND", "Archive does not exist.", path=archive_raw)
        archive_size = archive_path.stat().st_size
        if archive_size > MAX_IMAGE_ARCHIVE_BYTES:
            raise ToolError(
                "FILE_TOO_LARGE",
                f"Image archive exceeds {MAX_IMAGE_ARCHIVE_BYTES} bytes.",
                path=archive_raw,
                size=archive_size,
            )
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ZIP_MEMBERS:
                    raise ToolError("ZIP_TOO_MANY_MEMBERS", f"Archive exceeds {MAX_ZIP_MEMBERS} members.", path=archive_raw)
                try:
                    info = archive.getinfo(member)
                except KeyError as exc:
                    raise ToolError("NOT_FOUND", "Archive member does not exist.", path=archive_raw, member=member) from exc
                if info.is_dir():
                    raise ToolError("NOT_A_FILE", "Archive member is a directory.", path=archive_raw, member=member)
                if info.file_size > MAX_IMAGE_BYTES:
                    raise ToolError(
                        "IMAGE_TOO_LARGE",
                        f"Image member exceeds {MAX_IMAGE_BYTES} bytes.",
                        path=archive_raw,
                        member=member,
                        size=info.file_size,
                    )
                data = archive.read(info)
        except zipfile.BadZipFile as exc:
            raise ToolError("INVALID_ARCHIVE", "Image archive is not a readable ZIP file.", path=archive_raw) from exc
        source_meta = {
            "path": None,
            "archive_path": archive_path.relative_to(workspace.root).as_posix(),
            "member": member,
        }

    mime_type, width, height = _image_metadata(data)
    metadata = {
        **source_meta,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "mime_type": mime_type,
        "width": width,
        "height": height,
    }
    return {
        **metadata,
        "_content": [
            {"type": "text", "text": json.dumps(metadata, ensure_ascii=False, sort_keys=True)},
            {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime_type},
        ],
    }


def _page_range(page_start: int | None, page_end: int | None, slide_numbers: list[int]) -> tuple[int, int]:
    minimum = min(slide_numbers)
    maximum = max(slide_numbers)
    start = minimum if page_start is None else page_start
    end = maximum if page_end is None else page_end
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
        raise ToolError("INVALID_ARGUMENT", "page_start and page_end must be integers.")
    if start < minimum or end > maximum or start > end:
        raise ToolError("INVALID_ARGUMENT", f"Page range must stay within {minimum}..{maximum}.")
    if end - start + 1 > MAX_PPTX_PAGE_SPAN:
        raise ToolError("INVALID_ARGUMENT", f"A single PPTX inspection may cover at most {MAX_PPTX_PAGE_SPAN} pages.")
    return start, end


def _parse_smartart(archive: zipfile.ZipFile, data_part: str) -> dict[str, Any]:
    root = _read_xml_member(archive, data_part)
    points = root.findall(".//dgm:pt", _NS)
    connections = root.findall(".//dgm:cxn", _NS)
    if len(points) > MAX_SMARTART_ITEMS or len(connections) > MAX_SMARTART_ITEMS:
        raise ToolError(
            "SMARTART_TOO_COMPLEX",
            f"SmartArt exceeds {MAX_SMARTART_ITEMS} points or connections.",
            part=data_part,
        )
    return {
        "points": [
            {
                "attributes": {_local_name(key): value for key, value in point.attrib.items()},
                "text": _element_text(point),
            }
            for point in points
        ],
        "connections": [
            {"attributes": {_local_name(key): value for key, value in connection.attrib.items()}}
            for connection in connections
        ],
    }


def _slide_text(root: ET.Element) -> str:
    lines: list[str] = []
    for paragraph in root.findall(".//a:p", _NS):
        text = "".join(node.text or "" for node in paragraph.findall(".//a:t", _NS)).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _element_text(element: ET.Element) -> str:
    paragraphs = element.findall(".//a:p", _NS)
    if paragraphs:
        rendered: list[str] = []
        for paragraph in paragraphs:
            text = "".join(node.text or "" for node in paragraph.findall(".//a:t", _NS))
            if text:
                rendered.append(text)
        if rendered:
            return "\n".join(rendered)
    return "".join(node.text or "" for node in element.findall(".//a:t", _NS))


def _read_xml_member(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ToolError("MISSING_PACKAGE_PART", "Required OOXML part is missing.", part=name) from exc
    if info.file_size > MAX_XML_MEMBER_BYTES:
        raise ToolError(
            "XML_PART_TOO_LARGE",
            f"OOXML XML part exceeds {MAX_XML_MEMBER_BYTES} bytes.",
            part=name,
            size=info.file_size,
        )
    try:
        data = archive.read(info)
        return ET.fromstring(data)
    except (OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise ToolError("INVALID_OOXML", "Could not parse OOXML XML part.", part=name) from exc


def _normalize_package_target(base_dir: str, target: str) -> str:
    normalized_target = target.replace("\\", "/")
    if normalized_target.startswith("/"):
        normalized = posixpath.normpath(normalized_target.lstrip("/"))
    else:
        normalized = posixpath.normpath(posixpath.join(base_dir, normalized_target))
    if normalized == ".." or normalized.startswith("../"):
        raise ToolError("INVALID_OOXML", "OOXML relationship target escapes the package root.", target=target)
    return normalized


def _validate_archive_member(member: str) -> None:
    if "\x00" in member or "\\" in member:
        raise ToolError("INVALID_ARGUMENT", "Archive member must be a clean POSIX relative path.")
    path = PurePosixPath(member)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ToolError("INVALID_ARGUMENT", "Archive member must be a clean POSIX relative path.")


def _image_metadata(data: bytes) -> tuple[str, int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise ToolError("INVALID_IMAGE", "PNG header is incomplete.")
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", width, height
    if data.startswith((b"GIF87a", b"GIF89a")):
        if len(data) < 10:
            raise ToolError("INVALID_IMAGE", "GIF header is incomplete.")
        width, height = struct.unpack("<HH", data[6:10])
        return "image/gif", width, height
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", None, None
    if data.startswith(b"\xff\xd8"):
        width, height = _jpeg_dimensions(data)
        return "image/jpeg", width, height
    raise ToolError("UNSUPPORTED_IMAGE", "Only PNG, JPEG, GIF, and WebP images are exposed as image content.")


def _jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    index = 2
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while index + 3 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:
            break
        if index + 2 > len(data):
            break
        segment_length = int.from_bytes(data[index:index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            break
        if marker in sof_markers and segment_length >= 7:
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            return width, height
        index += segment_length
    return None, None


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise ToolError("READ_FAILED", f"Could not hash {path.name}: {exc}") from exc
    return digest.hexdigest()


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]

from __future__ import annotations

import base64
import binascii
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

import folderbridge_mcp.binary_tools as binary_tools_module
from folderbridge_mcp.binary_tools import file_info, inspect_pptx, open_image
from folderbridge_mcp.security import ToolError, Workspace


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
        )

    row = b"\x00" + b"\xff\xff\xff" * width
    raw = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class BinaryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.workspace = Workspace(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_file_info_hashes_binary(self) -> None:
        data = b"\x00\x01binary"
        (self.root / "sample.bin").write_bytes(data)
        info = file_info(self.workspace, "sample.bin")
        self.assertEqual(info["size"], len(data))
        self.assertEqual(len(info["sha256"]), 64)

    def test_pptx_inspection_maps_smartart(self) -> None:
        path = self.root / "sample.pptx"
        slide = b'''<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"
       xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:cSld><p:spTree>
    <p:sp><p:txBody><a:p><a:r><a:t>Hello slide</a:t></a:r></a:p></p:txBody></p:sp>
    <p:graphicFrame><a:graphic><a:graphicData><dgm:relIds r:dm="rId1"/></a:graphicData></a:graphic></p:graphicFrame>
  </p:spTree></p:cSld>
</p:sld>'''
        rels = b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
   Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramData"
   Target="../diagrams/data1.xml"/>
</Relationships>'''
        data = b'''<?xml version="1.0" encoding="UTF-8"?>
<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
 <dgm:ptLst>
  <dgm:pt modelId="A" type="node"><dgm:t><a:p><a:r><a:t>Node A</a:t></a:r></a:p></dgm:t></dgm:pt>
  <dgm:pt modelId="B" type="node"><dgm:t><a:p><a:r><a:t>Node B</a:t></a:r></a:p></dgm:t></dgm:pt>
 </dgm:ptLst>
 <dgm:cxnLst><dgm:cxn modelId="C" srcId="A" destId="B" srcOrd="0" destOrd="0"/></dgm:cxnLst>
</dgm:dataModel>'''
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ppt/slides/slide1.xml", slide)
            archive.writestr("ppt/slides/_rels/slide1.xml.rels", rels)
            archive.writestr("ppt/diagrams/data1.xml", data)

        result = inspect_pptx(self.workspace, "sample.pptx", page_start=1, page_end=1)
        self.assertEqual(result["slides"], 1)
        self.assertEqual(result["smartart_data_xml"], 1)
        self.assertEqual(result["smartart_frames"], 1)
        self.assertEqual(result["successful_diagram_mappings"], 1)
        expected_xml_bytes = len(slide) + len(rels) + len(data)
        self.assertEqual(result["xml_uncompressed_bytes"], expected_xml_bytes)
        self.assertEqual(result["xml_limit_bytes"], binary_tools_module.MAX_PPTX_XML_TOTAL_BYTES)
        self.assertAlmostEqual(
            result["xml_limit_usage_ratio"],
            expected_xml_bytes / binary_tools_module.MAX_PPTX_XML_TOTAL_BYTES,
        )
        self.assertEqual(result["orphan_data_parts"], [])
        self.assertIn("Hello slide", result["pages"][0]["slide_text"])
        smartart = result["pages"][0]["smartart_frames"][0]["smartart"]
        self.assertEqual([point["text"] for point in smartart["points"]], ["Node A", "Node B"])
        self.assertEqual(smartart["connections"][0]["attributes"]["srcId"], "A")
        self.assertEqual(smartart["connections"][0]["attributes"]["destId"], "B")

    def test_pptx_rejects_aggregate_xml_expansion_before_parsing(self) -> None:
        path = self.root / "compressed-bomb.pptx"
        slide = b'''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>bounded</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'''
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ppt/slides/slide1.xml", slide)
        with mock.patch.object(binary_tools_module, "MAX_PPTX_XML_TOTAL_BYTES", 100, create=True):
            with self.assertRaises(ToolError) as raised:
                inspect_pptx(self.workspace, "compressed-bomb.pptx", page_start=1, page_end=1)
        self.assertEqual(raised.exception.code, "PPTX_XML_TOO_LARGE")

    def test_image_open_direct_and_zip(self) -> None:
        image = _png(7, 5)
        (self.root / "slide.png").write_bytes(image)
        direct = open_image(self.workspace, raw="slide.png")
        self.assertEqual((direct["width"], direct["height"]), (7, 5))
        self.assertEqual(direct["mime_type"], "image/png")
        self.assertEqual(direct["_content"][1]["type"], "image")
        self.assertEqual(base64.b64decode(direct["_content"][1]["data"]), image)

        with zipfile.ZipFile(self.root / "render.zip", "w") as archive:
            archive.writestr("slide-56.png", image)
        archived = open_image(self.workspace, archive_raw="render.zip", member="slide-56.png")
        self.assertEqual((archived["width"], archived["height"]), (7, 5))

    def test_archive_member_traversal_is_rejected(self) -> None:
        with zipfile.ZipFile(self.root / "render.zip", "w") as archive:
            archive.writestr("slide.png", _png(1, 1))
        with self.assertRaises(ToolError):
            open_image(self.workspace, archive_raw="render.zip", member="../slide.png")


if __name__ == "__main__":
    unittest.main()

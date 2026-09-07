import tomllib
import unittest
from pathlib import Path

from folderbridge_mcp import __version__


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_package_and_windows_metadata_match_project_version(self) -> None:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
            project_version = tomllib.load(stream)["project"]["version"]

        self.assertEqual(project_version, "0.8.27")
        self.assertEqual(__version__, project_version)
        version_info = (PROJECT_ROOT / "packaging" / "windows_version_info.txt").read_text(encoding="utf-8")
        self.assertIn(f"filevers=(0, 8, 27, 0)", version_info)
        self.assertIn(f"prodvers=(0, 8, 27, 0)", version_info)
        self.assertIn(f"StringStruct('FileVersion', '{project_version}')", version_info)
        self.assertIn(f"StringStruct('ProductVersion', '{project_version}')", version_info)
        self.assertIn(
            f'version="{project_version}.0"',
            (PROJECT_ROOT / "packaging" / "windows_dpi_manifest.xml").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()

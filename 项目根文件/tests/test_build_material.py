"""Builder regressions; never call build() or touch the distribution tree."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts" / "build-material.py"
builder = None
if BUILDER_PATH.is_file():
    SPEC = importlib.util.spec_from_file_location("material_builder", BUILDER_PATH)
    builder = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(builder)


@unittest.skipIf(
    builder is None,
    "Maintainer-only builder is intentionally excluded from distribution",
)
class MaterialBuilderRegressionTests(unittest.TestCase):
    def test_example_banner_survives_sensitive_anchor_replacement(self):
        source = builder.BANNER_ANCHOR + "\nGROUP_GAG_RULES = {}\n"
        output, hits = builder.sanitize_text(source, "workbuddy_bridge/main.py")
        self.assertIn(builder.BANNER_TEXT, output)
        self.assertEqual(output.count(builder.BANNER_TEXT), 1)
        self.assertIn("banner", hits)
        self.assertNotIn(builder.BANNER_ANCHOR, output)

    def test_range_boundaries_are_checked_instead_of_matching_none(self):
        fixtures = [
            ((172, 15, 0, 1), False),
            ((172, 16, 0, 1), True),
            ((172, 31, 255, 255), True),
            ((172, 32, 0, 1), False),
            ((100, 63, 255, 255), False),
            ((100, 64, 0, 1), True),
            ((100, 127, 255, 255), True),
            ((100, 128, 0, 1), False),
            ((127, 0, 0, 1), True),
            ((192, 168, 1, 1), True),
            ((169, 254, 1, 1), True),
            ((172, 16, 999, 1), False),
            ((255, 255, 255, 255), True),
            ((255, 255, 1, 1), False),
        ]
        for address, expected in fixtures:
            with self.subTest(address=address):
                self.assertEqual(
                    builder._is_private_ip(*(str(part) for part in address)), expected
                )

    def test_both_profiles_include_all_runtime_sources_and_maintenance_dependencies(
        self,
    ):
        required = {
            "workbuddy_bridge/requirements.txt": "AstrBot/data/plugins/workbuddy_bridge/requirements.txt",
            "workbuddy_bridge/request_queue.py": "AstrBot/data/plugins/workbuddy_bridge/request_queue.py",
            "workbuddy_bridge/image_jobs.py": "AstrBot/data/plugins/workbuddy_bridge/image_jobs.py",
            "workbuddy_bridge/jimeng.py": "AstrBot/data/plugins/workbuddy_bridge/jimeng.py",
            "workbuddy_bridge/prompts.py": "AstrBot/data/plugins/workbuddy_bridge/prompts.py",
            "workbuddy_bridge/reply_text.py": "AstrBot/data/plugins/workbuddy_bridge/reply_text.py",
            "workbuddy_bridge/source_files.py": "AstrBot/data/plugins/workbuddy_bridge/source_files.py",
            "项目根文件/scripts/diagnostic_evidence.py": "scripts/diagnostic_evidence.py",
            "项目根文件/scripts/reset-astrbot-password.py": "scripts/reset-astrbot-password.py",
            "项目根文件/README.md": "README.md",
        }
        for development in (False, True):
            with self.subTest(development=development):
                manifest = dict(builder.text_files(development))
                for destination, source in required.items():
                    self.assertEqual(manifest.get(destination), source)
                for name in builder._inventory.SOURCE_NAMES:
                    self.assertEqual(
                        manifest.get("workbuddy_bridge/" + name),
                        "AstrBot/data/plugins/workbuddy_bridge/" + name,
                    )

    def test_runtime_profile_contains_exactly_selfcheck_dependencies(self):
        sources = {source for _, source in builder.text_files()}
        expected = {"tests/bridge_selftest.py"} | {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "tests/selfcheck").rglob("*.py")
        }
        self.assertEqual(
            {
                source
                for source in sources
                if source.startswith("tests/") and source.endswith(".py")
            },
            expected,
        )
        self.assertNotIn("tests/bridge_test_support.py", sources)
        self.assertNotIn("tests/test_question_scheduling.py", sources)
        self.assertIn("tests/bridge_selftest.py", sources)

    def test_development_profile_contains_every_test_and_common_helper(self):
        manifest = dict(builder.text_files(development=True))
        for source in (ROOT / "tests").rglob("*.py"):
            relative = source.relative_to(ROOT).as_posix()
            self.assertEqual(manifest.get("项目根文件/" + relative), relative)

    def test_profiles_find_new_nested_selfcheck_files_without_duplicate_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            for relative in (
                "tests/bridge_selftest.py",
                "tests/selfcheck/__init__.py",
                "tests/selfcheck/support.py",
                "tests/selfcheck/rules/chat.py",
                "tests/test_development.py",
                "tests/bridge_test_support.py",
            ):
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# fixture\n", encoding="utf-8")
            with patch.object(builder, "HERE", project):
                runtime = builder.text_files()
                development = builder.text_files(True)
                for manifest in (runtime, development):
                    self.assertEqual(len(manifest), len(dict(manifest)))
                    self.assertIn(
                        (
                            "项目根文件/tests/selfcheck/rules/chat.py",
                            "tests/selfcheck/rules/chat.py",
                        ),
                        manifest,
                    )
                self.assertEqual(len(development) - len(runtime), 2)

    def test_profiles_have_separate_outputs_and_one_deployment_guide(self):
        runtime, runtime_zip = builder.material_paths()
        development, development_zip = builder.material_paths(True)
        self.assertTrue(runtime.is_absolute())
        self.assertEqual(runtime.parent, ROOT / "dist")
        self.assertNotEqual(runtime, development)
        self.assertNotEqual(runtime_zip, development_zip)
        for profile in (False, True):
            manifest = builder.text_files(profile)
            self.assertEqual(
                [
                    destination
                    for destination, source in manifest
                    if source == "docs/部署指南.md"
                ],
                ["项目根文件/docs/部署指南.md"],
            )
        self.assertIn("项目根文件/docs/部署指南.md", builder.PACKAGE_README)

    def test_material_does_not_include_credentials_or_builder_mapping(self):
        forbidden_sources = (
            "workbuddy-gateway.env",
            "scripts/build-material.py",
            "AstrBot/data/plugins/workbuddy_bridge/draw_jobs",
            "AstrBot/data/plugins/workbuddy_bridge/question_run.json",
            "AstrBot/data/plugins/workbuddy_bridge/bridge_status.json",
            "AstrBot/data/plugins/workbuddy_bridge/memory.sqlite3",
            "AstrBot/data/plugins/workbuddy_bridge/memory.sqlite3-wal",
            "AstrBot/data/plugins/workbuddy_bridge/memory.sqlite3-shm",
            "hist.json",
            "sess.json",
        )
        for profile in (False, True):
            sources = {
                source for _, source in builder.text_files(profile) + builder.COPY_FILES
            }
            for forbidden in forbidden_sources:
                self.assertNotIn(forbidden, sources)


if __name__ == "__main__":
    unittest.main()

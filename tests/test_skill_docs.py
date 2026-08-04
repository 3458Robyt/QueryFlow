import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/queryflow/skills/queryflow/SKILL.md"
REFERENCES = ROOT / "plugins/queryflow/skills/queryflow/references"
DOCS = ROOT / "docs"


class SkillDocumentationTests(unittest.TestCase):
    def test_skill_has_trigger_metadata_and_complete_workflow(self):
        text = SKILL.read_text(encoding="utf-8")

        self.assertRegex(text, re.compile(r"^description: Use when .+$", re.MULTILINE))
        for command in ("catalog", "start", "validate", "review", "sample", "publish"):
            self.assertIn(f"queryflow {command}", text)
        for reference in ("commands.md", "workflows.md", "troubleshooting.md", "security.md"):
            self.assertIn(f"references/{reference}", text)
        self.assertLessEqual(len(text.splitlines()), 500)

    def test_agent_references_and_human_manuals_exist(self):
        for name in ("commands.md", "workflows.md", "troubleshooting.md", "security.md"):
            self.assertTrue((REFERENCES / name).is_file(), name)
        for name in ("USER_GUIDE.md", "COMMAND_REFERENCE.md", "TROUBLESHOOTING.md"):
            self.assertTrue((DOCS / name).is_file(), name)

    def test_command_reference_covers_public_cli_surface(self):
        text = (DOCS / "COMMAND_REFERENCE.md").read_text(encoding="utf-8")
        for command in (
            "version",
            "init",
            "config",
            "policy",
            "install",
            "doctor",
            "catalog",
            "profile",
            "start",
            "validate",
            "sample",
            "review",
            "publish",
        ):
            self.assertRegex(text, rf"`queryflow {command}(?: |`)")

    def test_public_docs_do_not_contain_operational_identifiers(self):
        paths = [SKILL, *REFERENCES.glob("*.md"), *DOCS.glob("*.md")]
        prohibited = (
            re.compile(r"analytics-\d+", re.IGNORECASE),
            re.compile(r"sbscol-[a-z0-9-]+", re.IGNORECASE),
            re.compile(r"-----BEGIN .*PRIVATE KEY-----"),
            re.compile(r"(?:ghp|github_pat|AIza|xoxb)-[A-Za-z0-9_-]+"),
        )
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for pattern in prohibited:
                self.assertIsNone(pattern.search(text), f"{pattern.pattern} in {path}")


if __name__ == "__main__":
    unittest.main()

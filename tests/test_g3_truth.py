from __future__ import annotations

import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class G3TruthTests(unittest.TestCase):
    def test_unwired_money_tightener_declarations_are_absent(self) -> None:
        source = (REPO_ROOT / "lead_machine" / "sendability.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        assigned_names = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name)
        }
        function_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertNotIn("MONEY_SIGNAL_BLOCKER", assigned_names)
        self.assertNotIn("is_bad_site_only_signal", function_names)

    def test_docs_and_verifier_skill_do_not_claim_the_tightener_is_live(self) -> None:
        paths_and_forbidden_claims = {
            "docs/phase-a-backlog.md": (
                "MONEY_SIGNAL_BLOCKER",
                "sendability money-gate",
                "bad-site-no-money leads cap out",
            ),
            "docs/phase-b-c-roadmap.md": ("sendability money gate",),
            "docs/outreach-loop-strategy.md": ("MONEY_SIGNAL_BLOCKER",),
            ".claude/skills/verify-batch/SKILL.md": (
                "MONEY_SIGNAL_BLOCKER",
                "once G3 lands",
                "(post-G3)",
            ),
        }

        for relative_path, forbidden_claims in paths_and_forbidden_claims.items():
            with self.subTest(path=relative_path):
                text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
                for claim in forbidden_claims:
                    self.assertNotIn(claim, text)

        skill = (REPO_ROOT / ".claude/skills/verify-batch/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Missing money signal is not a standalone blocker", skill)


if __name__ == "__main__":
    unittest.main()

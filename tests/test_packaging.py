from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
DASHBOARD_REQUIREMENTS = REPO_ROOT / "requirements-dashboard.txt"


def read_extra(name: str) -> list[str]:
    """Return the requirement strings in one `[project.optional-dependencies]` extra.

    Hand-rolled rather than `tomllib` on purpose: the pinned local interpreter is
    3.10 (CI is 3.12), so a `tomllib` import would pass in CI and error on the
    command AGENTS.md tells you to run.
    """
    lines = PYPROJECT.read_text(encoding="utf-8").splitlines()
    collected: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if not inside:
            if stripped.startswith(f"{name} = ["):
                inside = True
            continue
        if stripped == "]":
            return collected
        collected.append(stripped.rstrip(",").strip('"'))
    raise AssertionError(f"pyproject.toml has no unterminated-free extra named {name!r}")


class PackagingContractTests(unittest.TestCase):
    def test_dashboard_extra_matches_requirements_file(self) -> None:
        """The dashboard deps are declared twice; drift is the lxml gate all over again.

        `requirements-dashboard.txt` still exists because `docs/dashboard.md`
        tells operators to install from it, while CI now installs the same set
        from the `dashboard` extra. Until one of them is retired, a dep added to
        either has to be added to both.
        """
        from_file = [
            line.strip()
            for line in DASHBOARD_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(sorted(read_extra("dashboard")), sorted(from_file))

    def test_test_extra_covers_the_html_parsing_deps(self) -> None:
        """lxml missing from the test install is a shipped CI failure, not a hypothetical.

        The 2026-08-01 send-readiness audit found it red. Pin the pair here so a
        future edit to the extra cannot drop it silently.
        """
        extra = set(read_extra("test"))
        self.assertIn("beautifulsoup4", extra)
        self.assertIn("lxml", extra)

    def test_runtime_extra_covers_the_gate_scripts(self) -> None:
        """gates.yml installs `[runtime]` only, and cannot be verified by a branch push.

        It runs on a self-hosted runner from schedule/workflow_dispatch, so an
        import it can no longer satisfy surfaces at 11:37 UTC rather than in a
        PR. These are the third-party imports its entry points reach.
        """
        extra = {requirement.split(">=")[0].split("[")[0] for requirement in read_extra("runtime")}
        self.assertIn("requests", extra)
        self.assertIn("python-dotenv", extra)


if __name__ == "__main__":
    unittest.main()

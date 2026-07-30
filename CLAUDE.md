# CLAUDE.md

See **[AGENTS.md](AGENTS.md)** for repo orientation and conventions.

**[docs/phase-a-backlog.md](docs/phase-a-backlog.md)** (Phase A — find good leads + run without you)
is the design record for the pipeline as built. G1→G4 all shipped; it is no longer a to-do list, but
it still holds the reasoning behind scoring, enrichment, ICP, search configs and the pipeline, so
read it before changing any of them. Its "Landed since" section carries the correctness rules added
after G4. Next build order → **[docs/phase-b-c-roadmap.md](docs/phase-b-c-roadmap.md)**.

Keep every change a small, independently-revertible batch verified with
`.venv/bin/python -m unittest discover -s tests` (bare `python -m unittest` discovers zero tests
here and reports OK).

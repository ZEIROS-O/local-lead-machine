from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .api import create_app
from .data import default_repo_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the read-only Local Lead Machine dashboard")
    parser.add_argument("--repo-root", default=str(default_repo_root()))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    app = create_app(Path(args.repo_root))
    uvicorn.run(app, host=args.host, port=args.port, access_log=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

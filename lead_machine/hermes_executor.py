from __future__ import annotations

import os
import selectors
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass
class StreamResult:
    returncode: int
    timed_out: bool
    tail_lines: list[str]


def render_command(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in argv)


def run_streaming(
    argv: Sequence[str],
    *,
    timeout_seconds: float | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    resume_hint: str = "",
    tail_limit: int = 80,
) -> StreamResult:
    merged_env = os.environ.copy()
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})

    if cwd is not None:
        print(f"repo_root: {Path(cwd)}", flush=True)
    print(f"[RUN] {render_command(argv)}", flush=True)
    proc = subprocess.Popen(
        [str(arg) for arg in argv],
        cwd=str(cwd) if cwd is not None else None,
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    tail: list[str] = []
    deadline = time.monotonic() + timeout_seconds if timeout_seconds else None
    timed_out = False

    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            wait = 0.1
            if deadline is not None:
                wait = max(0.0, min(wait, deadline - time.monotonic()))
            for key, _mask in selector.select(wait):
                line = key.fileobj.readline()
                if line:
                    clean = line.rstrip("\n")
                    print(clean, flush=True)
                    tail.append(clean)
                    if len(tail) > tail_limit:
                        tail = tail[-tail_limit:]

            if proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    for clean in rest.splitlines():
                        print(clean, flush=True)
                        tail.append(clean)
                    tail = tail[-tail_limit:]
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                print(f"[TIMEOUT] Command exceeded {timeout_seconds:.0f}s", flush=True)
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                if resume_hint:
                    print(f"[RESUME] {resume_hint}", flush=True)
                break
    finally:
        selector.close()
        proc.stdout.close()

    return StreamResult(
        returncode=proc.returncode if proc.returncode is not None else 124,
        timed_out=timed_out,
        tail_lines=tail[-tail_limit:],
    )

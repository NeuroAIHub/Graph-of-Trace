"""Concurrency test for the Graph-of-Trace writer.

Verifies that two concurrent build_trace calls for the SAME session are
serialized (queued in arrival order) rather than interleaving or deadlocking
the event loop, and that both resulting nodes are persisted without one
clobbering the other.

Run:
    python -m pytest tests/test_concurrency.py
    python tests/test_concurrency.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _run() -> None:
    tmp = tempfile.mkdtemp(prefix="got_conc_")

    from Monitor.config import parser as cfg_parser

    _orig_get_output = cfg_parser.get_output_config

    def _patched_output(cfg):
        return {
            "base_dir": tmp,
            "path_template": "{base_dir}/{project_name}/{session_id}/got.json",
        }

    cfg_parser.get_output_config = _patched_output

    from Monitor import steps_llm

    # Records whether two calls' critical sections overlapped.
    active = {"n": 0, "overlap": False}
    order: list[str] = []

    async def _fake_build_nodes(*, session_id, subtask, artifacts, steps):
        active["n"] += 1
        if active["n"] > 1:
            active["overlap"] = True
        order.append(subtask["title"])
        # Yield control: if the lock did NOT serialize, the second coroutine
        # would enter here too and active["n"] would exceed 1.
        await asyncio.sleep(0.05)
        active["n"] -= 1
        # Parent on the latest existing node so we can tell ordering held:
        existing = [n["id"] for n in steps["nodes"]]
        new_id = f"N{len(existing) + 1:03d}"
        return [
            {
                "id": new_id,
                "title": subtask["title"],
                "description": subtask.get("description", ""),
                "parents": [{"id": existing[-1], "relation": "necessitated_by"}],
            }
        ]

    steps_llm.build_nodes = _fake_build_nodes

    from Monitor.got_writer import write_got_from_build_trace, _resolve_got_path

    project_name = "conc-proj"
    session_id = "sess-conc"

    def _payload(title: str):
        return {
            "project": {"name": project_name},
            "session": {"id": session_id},
            "subtask": {"title": title, "description": title},
            "artifacts": [],
        }

    async def _drive():
        # Fire both concurrently for the same session.
        return await asyncio.gather(
            write_got_from_build_trace(
                project_name=project_name, session_id=session_id, payload=_payload("step A")
            ),
            write_got_from_build_trace(
                project_name=project_name, session_id=session_id, payload=_payload("step B")
            ),
        )

    results = asyncio.run(_drive())

    assert all(r["status"] == "ok" for r in results), results
    assert active["overlap"] is False, "critical sections overlapped — not serialized"

    got_path = _resolve_got_path(project_name, session_id)
    data = json.loads(got_path.read_text(encoding="utf-8"))
    ids = [n["id"] for n in data["nodes"]]
    titles = [n["title"] for n in data["nodes"]]
    # Root + both appended nodes survive (no lost update).
    assert ids == ["N001", "N002", "N003"], ids
    assert "step A" in titles and "step B" in titles, titles

    cfg_parser.get_output_config = _orig_get_output
    print(f"OK concurrency test passed (order={order}) -> {got_path}")


def test_concurrency():
    _run()


if __name__ == "__main__":
    _run()

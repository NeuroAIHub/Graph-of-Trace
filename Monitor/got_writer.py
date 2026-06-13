"""Persist build_trace payload into got.json via LLM-generated nodes only."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("mcp_tools.monitor")

# In-process, per-session locks. asyncio.Lock is FIFO/fair, so concurrent
# build_trace calls for the SAME session queue and complete in arrival order
# instead of racing. Keyed by the resolved got.json path (one graph == one key).
# Cross-process safety is handled separately by the fcntl file lock below.
_session_locks: Dict[str, "asyncio.Lock"] = {}

# How long to wait for the cross-process file lock before giving up. Worst-case
# a single in-flight LLM call is ~90s with retries; allow queueing room on top.
_FLOCK_TIMEOUT_SECONDS = 120.0
_FLOCK_POLL_SECONDS = 0.05


def _get_session_lock(key: str) -> "asyncio.Lock":
    """Return the asyncio.Lock for this session, creating it on first use.

    Safe without its own guard: the event loop is single-threaded and there is
    no await between the get and the set, so two coroutines cannot interleave
    here and create two different locks for the same key.
    """
    lock = _session_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[key] = lock
    return lock


async def _acquire_flock_async(
    lock_f,
    *,
    timeout: float = _FLOCK_TIMEOUT_SECONDS,
    poll: float = _FLOCK_POLL_SECONDS,
) -> None:
    """Acquire an exclusive fcntl lock without blocking the event loop.

    Uses LOCK_NB and polls with asyncio.sleep so that while we wait for another
    process to release the lock, this loop stays free to run other coroutines
    (e.g. an in-flight LLM call whose callback must fire). A blocking flock here
    would freeze the whole single-threaded loop and could deadlock.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"got.json file lock busy for >{timeout:.0f}s; giving up"
                )
            await asyncio.sleep(poll)


@asynccontextmanager
async def _session_write_lock(lock_path: Path, session_key: str):
    """Serialize writes to one session's got.json, in arrival order.

    Two layers:
    - in-process: a per-session asyncio.Lock (FIFO) makes concurrent
      build_trace calls for the same session queue and run one at a time,
      without blocking the event loop.
    - cross-process: a non-blocking fcntl lock guards against other processes
      writing the same file. Within one process the asyncio.Lock means the
      fcntl lock is essentially always free on first try.
    """
    async with _get_session_lock(session_key):
        with lock_path.open("w") as lock_f:
            await _acquire_flock_async(lock_f)
            try:
                yield
            finally:
                # Released implicitly when lock_f closes, but be explicit.
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)



def _parents_by_id(nodes: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for n in nodes:
        nid = n.get("id")
        if not isinstance(nid, str) or not nid.strip():
            continue
        parents = n.get("parents")
        if not isinstance(parents, list):
            continue
        pids: List[str] = []
        for p in parents:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            if isinstance(pid, str) and pid.strip():
                pids.append(pid.strip())
        out[nid.strip()] = pids
    return out


def _is_reachable_parent(
    *,
    start_id: str,
    target_id: str,
    parents_by_id: Dict[str, List[str]],
) -> bool:
    if start_id == target_id:
        return True
    seen = set([start_id])
    stack = [start_id]
    while stack:
        cur = stack.pop()
        for nxt in parents_by_id.get(cur, []):
            if nxt == target_id:
                return True
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def _dedupe_redundant_parents(
    *,
    nodes: List[Dict[str, Any]],
    new_node_ids: List[str],
) -> int:
    """Remove redundant parents for newly appended nodes.

    Redundant means: if a node has parents [A, B] and A is an ancestor of B,
    then A is removed (keep the closer parent B).

    Only touches nodes whose id is in new_node_ids.
    """

    parents_by_id = _parents_by_id(nodes)
    removed = 0

    new_id_set = {x for x in new_node_ids if isinstance(x, str) and x.strip()}
    if not new_id_set:
        return 0

    for n in nodes:
        nid = n.get("id")
        if nid not in new_id_set:
            continue
        parents = n.get("parents")
        if not isinstance(parents, list) or len(parents) <= 1:
            continue

        # 1) de-dup by id (stable: keep first occurrence)
        seen_parent_ids = set()
        unique_parents: List[Dict[str, Any]] = []
        for p in parents:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            if not isinstance(pid, str) or not pid.strip():
                continue
            pid = pid.strip()
            if pid in seen_parent_ids:
                removed += 1
                continue
            seen_parent_ids.add(pid)
            p["id"] = pid
            unique_parents.append(p)

        if len(unique_parents) <= 1:
            n["parents"] = unique_parents
            continue

        parent_ids = [p["id"] for p in unique_parents if isinstance(p.get("id"), str)]
        redundant_ids = set()

        # If Pj can reach Pi via parent edges, then Pj is an ancestor of Pi -> Pj is redundant.
        for i in range(len(parent_ids)):
            for j in range(len(parent_ids)):
                if i == j:
                    continue
                pj = parent_ids[j]
                pi = parent_ids[i]
                if pj in redundant_ids:
                    continue
                if _is_reachable_parent(start_id=pj, target_id=pi, parents_by_id=parents_by_id):
                    redundant_ids.add(pj)

        if redundant_ids and len(redundant_ids) < len(unique_parents):
            n["parents"] = [p for p in unique_parents if p.get("id") not in redundant_ids]
            removed += len(redundant_ids)
        else:
            n["parents"] = unique_parents

    return removed


def _sanitize_path_segment(value: str) -> str:
    """Make an agent-supplied identifier safe to use as a path segment.

    Strips path separators and parent refs so project_name / session_id can
    never escape the configured base_dir.
    """
    s = (value or "").strip()
    s = s.replace("\\", "/")
    parts = [p for p in s.split("/") if p and p not in (".", "..")]
    cleaned = "_".join(parts) if parts else "unknown"
    return cleaned


def _resolve_got_path(project_name: str, session_id: str) -> Path:
    """Resolve the got.json path from the configured output template.

    Path layout is fully agent-agnostic and driven by config `output`
    (base_dir + path_template). See Monitor/config/config.yaml.
    """
    from .config.parser import load_config, get_output_config

    cfg = load_config()
    oc = get_output_config(cfg)

    base_dir = os.path.expanduser(oc["base_dir"])
    rendered = oc["path_template"].format(
        base_dir=base_dir,
        project_name=_sanitize_path_segment(project_name),
        session_id=_sanitize_path_segment(session_id),
    )
    return Path(os.path.expanduser(rendered))


def _load_or_init(got_path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(got_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {"meta": {}, "nodes": []}
    except json.JSONDecodeError:
        data = {"meta": {}, "nodes": []}

    meta = data.get("meta") or {}
    nodes = data.get("nodes") or []
    if not isinstance(meta, dict):
        meta = {}
    if not isinstance(nodes, list):
        nodes = []

    data["meta"] = meta
    data["nodes"] = nodes
    return data


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with tmp.open("r+") as f:
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


async def write_got_from_build_trace(
    *,
    project_name: str,
    session_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Load got.json, call LLM to generate nodes from subtask (+ artifacts), append, save.

    - Path: resolved from config `output` (base_dir + path_template); agent-agnostic.
    - Uses fcntl lock.
    """
    from .steps_llm import build_nodes

    got_path = _resolve_got_path(project_name, session_id)
    session_dir = got_path.parent
    session_dir.mkdir(parents=True, exist_ok=True)

    lock_path = session_dir / f"{got_path.name}.lock"

    async with _session_write_lock(lock_path, session_key=str(got_path)):

        got = _load_or_init(got_path)
        meta: Dict[str, Any] = got["meta"]
        nodes: List[Dict[str, Any]] = got["nodes"]

        log.info(
            "got_writer loaded path=%s existing_nodes=%d meta_keys=%s",
            str(got_path),
            len(nodes),
            sorted(list(meta.keys())),
        )

        meta.setdefault("project_name", project_name)
        meta.setdefault("session_id", session_id)


        if not nodes:
            nodes.append(
                {
                    "id": "N001",
                    "title": "Session start",
                    "description": "Root node for this session.",
                    "parents": [{"id": "N001", "relation": "necessitated_by"}],
                }
            )

        steps_dict: Dict[str, Any] = {"meta": meta, "nodes": nodes}
        primary_node_id: Optional[str] = None

        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("payload.artifacts must be a list")

        subtask = payload.get("subtask")
        if not isinstance(subtask, dict) or not subtask:
            raise ValueError("payload.subtask is required")

        # Back-compat / boundary validation: depends_on is optional but must be a list of strings if present.
        depends_on = subtask.get("depends_on")
        if depends_on is not None and not (
            isinstance(depends_on, list) and all(isinstance(x, str) for x in depends_on)
        ):
            raise ValueError("subtask.depends_on must be a list of strings")

        log.info(
            "got_writer building nodes from subtask keys=%s artifacts=%d",
            sorted(list(subtask.keys())),
            len(artifacts),
        )

        new_nodes = await build_nodes(
            session_id=session_id,
            subtask=subtask,
            artifacts=artifacts,
            steps=steps_dict,
        )

        log.info("got_writer nodes_from_subtask raw_count=%d", len(new_nodes or []))
        if new_nodes:
            nodes.extend(new_nodes)
            primary_node_id = new_nodes[-1].get("id")

            new_node_ids = [n.get("id") for n in new_nodes if isinstance(n, dict)]
            removed = _dedupe_redundant_parents(nodes=nodes, new_node_ids=new_node_ids)
            if removed:
                log.info(
                    "got_writer removed redundant parents removed=%d new_nodes=%d",
                    removed,
                    len(new_nodes),
                )
        else:
            # No nodes produced. By design the writer LLM may reject a subtask it
            # judges not node-worthy (maintenance/debug); see steps_llm logs for
            # whether this was a policy rejection or an extraction error. Either way
            # the agent is told status=ok and got.json is unchanged except meta.
            log.info("got_writer no nodes added for this subtask; got.json unchanged except meta")

        log.info(
            "got_writer built nodes_from_subtask added=%d primary_node_id=%s",
            len(new_nodes or []),
            primary_node_id or "",
        )


        log.info(
            "got_writer writing path=%s total_nodes=%d primary_node_id=%s",
            str(got_path),
            len(nodes),
            primary_node_id or "",
        )
        _atomic_write_json(got_path, got)
        log.info("got_writer write complete path=%s", str(got_path))
        return {
            "status": "ok",
            "primary_node_id": primary_node_id or "",
            "nodes_added": len(new_nodes or []),
            "deduped": False,
        }

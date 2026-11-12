# -*- coding: utf-8 -*-

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from bili_digest import paths

def jobs_dir() -> Path:
    d = Path(paths.DATA_DIR) / ".cache" / "ingest"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _state_path(job_id: str) -> Path:
    return jobs_dir() / job_id / "state.json"

def _read(job_id: str) -> dict:
    p = _state_path(job_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _write(job_id: str, st: dict) -> None:
    p = _state_path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

def new_job_id(bvid: str) -> str:
    return "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), bvid[-6:])

def _pipeline(bvid: str, refine: bool) -> list:
    py = sys.executable
    steps = [
        ("fetch", [py, "-m", "bili_digest", "fetch", bvid]),

        ("seg", [py, "-m", "bili_digest.distill.seg_boundary", "--build"]),
    ]
    if refine:
        steps.append(("derive", [py, "-m", "bili_digest", "derive",
                                 "--uid", "bilibili:" + bvid]))
    steps.append(("index", [py, "-m", "bili_digest", "index"]))
    return steps

def start(bvid: str, refine: bool = True) -> dict:
    job_id = new_job_id(bvid)
    d = jobs_dir() / job_id
    d.mkdir(parents=True, exist_ok=True)
    log = d / "log.txt"
    steps = _pipeline(bvid, refine)
    st = {
        "job_id": job_id,
        "bvid": bvid,
        "refine": refine,
        "status": "running",
        "step": steps[0][0],
        "steps": [s[0] for s in steps],
        "done_steps": [],
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "log": str(log),
    }
    _write(job_id, st)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("=== %s %s ===\n" % (st["started_at"], job_id))

    root = str(paths.HERE)
    runner = [sys.executable, "-c", _CHILD % (job_id, bvid, refine, root)]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    subprocess.Popen(runner, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, cwd=root, **kw)
    return st

_CHILD = ("from bili_digest.ingest.ingest_job import child_run; "
          "child_run(%r, %r, %r, %r)")

def child_run(job_id: str, bvid: str, refine: bool, root: str) -> None:
    import time
    log = jobs_dir() / job_id / "log.txt"
    st = _read(job_id)
    for name, cmd in _pipeline(bvid, refine):
        st.update(step=name, status="running")
        _write(job_id, st)
        t0 = time.time()
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("--- %s start ---\n" % name)
            r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=root)
            fh.write("--- %s rc=%d %.1fs ---\n" % (name, r.returncode, time.time() - t0))
        if r.returncode != 0:
            st.update(status="failed", failed_step=name,
                      finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            _write(job_id, st)
            return
        st.setdefault("done_steps", []).append(name)
        _write(job_id, st)
    st.update(status="done", step="",
              finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    _write(job_id, st)

def status(job_id: str, tail: int = 12) -> dict:
    st = _read(job_id)
    if not st:
        return {"error": "没有这个任务：%s" % job_id}
    tail_lines = []
    p = Path(st.get("log", ""))
    if p.exists():
        try:
            alll = p.read_text(encoding="utf-8", errors="replace").splitlines()
            tail_lines = alll[-tail:]
        except Exception:
            pass
    st["log_tail"] = tail_lines

    if st.get("status") == "running":
        import time as _t
        last = None
        try:
            if p.exists():
                last = p.stat().st_mtime
        except Exception:
            pass
        if last is None:
            last = st.get("started_at") or st.get("ts")
        if isinstance(last, str):
            try:
                last = float(last)
            except Exception:
                last = None
        if last:
            idle = _t.time() - float(last)
            st["idle_sec"] = round(idle, 1)

            if idle > 900:
                st["status"] = "stalled"
                st["hint"] = ("已 %d 分钟没有日志变化，进程可能已死。"
                              "可 kb_task_status 确认后重跑，或 kb_ingest_cancel 清理。" % int(idle / 60))
    return st

def cancel(job_id: str) -> dict:
    st = _read(job_id)
    if not st:
        return {"error": "没有这个任务：%s" % job_id}
    if st.get("status") in ("done", "failed", "cancelled"):
        return st
    pid = st.get("pid")
    if pid:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True)
            else:
                os.kill(int(pid), 15)
        except Exception:
            pass
    st.update(status="cancelled", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    _write(job_id, st)
    return st

def list_jobs(limit: int = 10) -> list:
    d = jobs_dir()
    out = []
    for p in sorted(d.iterdir(), reverse=True)[:limit]:
        st = _read(p.name)
        if st:
            out.append(st)
    return out

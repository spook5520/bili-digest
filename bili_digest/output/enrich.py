import sys, json, sqlite3

def chapters(con, item_uid, window: int = 300, max_n: int = 16, min_seg: int = 2):
    rows = list(con.execute(
        "SELECT offset_sec, text FROM content_block "
        "WHERE item_uid=? AND kind='subtitle_seg' ORDER BY offset_sec",
        (item_uid,)))
    if not rows:
        return []

    if len(rows) < max(4, min_seg * 2):
        return []
    span = (rows[-1]["offset_sec"] or 0) - (rows[0]["offset_sec"] or 0)
    w = max(window, int(span / max_n) if max_n else window)
    buckets = {}
    for r in rows:
        t = int((r["offset_sec"] or 0) // w) * w
        buckets.setdefault(t, []).append(r)
    out = []
    for t in sorted(buckets):
        grp = buckets[t]
        if len(grp) < min_seg:
            continue
        best = max(grp, key=lambda x: len((x["text"] or "").strip()))
        txt = " ".join((best["text"] or "").split())[:96]
        if txt:
            out.append({"t": t, "text": txt, "n": len(grp)})
    return out

def ensure_meta(con, item_uid, timeout: float = 15.0) -> dict:
    row = con.execute("SELECT item_id, extras FROM item WHERE uid=?", (item_uid,)).fetchone()
    if not row:
        return {}
    try:
        cur = json.loads(row["extras"] or "{}")
    except (ValueError, TypeError):
        cur = {}
    if cur.get("cid") and cur.get("tname"):
        return cur
    try:
        from bili_digest.adapters.adapters import get_adapter
        ad = get_adapter("bilibili")
        meta = ad.fetch_item(row["item_id"]) or {}
        ex = meta.get("extras") or {}
        add = {k: ex[k] for k in ("aid", "cid", "tname", "tname_v2", "desc", "view")
               if ex.get(k) is not None and cur.get(k) is None}
        if not add:
            return cur
        cur.update(add)
        con.execute("UPDATE item SET extras=? WHERE uid=?",
                    (json.dumps(cur, ensure_ascii=False), item_uid))
        con.commit()
        return cur
    except Exception:
        return cur

def enrich(con, item_uid, online: bool = False) -> dict:
    out = {"chapters": chapters(con, item_uid)}
    if online:
        out["meta"] = ensure_meta(con, item_uid)
    return out

import os as _os
import sys as _sys

from bili_digest import paths

import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

def form_filter(con, uids, min_duration=None, max_duration=None,
                min_blocks=None, max_blocks=None) -> set:
    if not uids:
        return set()
    if not any(v is not None for v in (min_duration, max_duration,
                                       min_blocks, max_blocks)):
        return set(uids)
    ph = ",".join("?" * len(uids))
    where, args = [], list(uids)
    if min_duration is not None:
        where.append("duration_sec IS NOT NULL AND duration_sec >= ?")
        args.append(min_duration)
    if max_duration is not None:
        where.append("duration_sec IS NOT NULL AND duration_sec <= ?")
        args.append(max_duration)
    if min_blocks is not None:
        where.append("(SELECT COUNT(*) FROM content_block cb WHERE cb.item_uid=item.uid) >= ?")
        args.append(min_blocks)
    if max_blocks is not None:
        where.append("(SELECT COUNT(*) FROM content_block cb WHERE cb.item_uid=item.uid) <= ?")
        args.append(max_blocks)
    sql = (f"SELECT uid FROM item WHERE uid IN ({ph}) AND " + " AND ".join(where))
    return {r[0] for r in con.execute(sql, args)}

if __name__ == "__main__":
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    con = connect(DB)
    con.row_factory = sqlite3.Row
    allu = [r["uid"] for r in con.execute("SELECT uid FROM item")]
    print(f"  条目 {len(allu)}\n")
    print("  形式过滤的实测:")
    for label, kw in (
        ("时长 > 10 分钟",    dict(min_duration=600)),
        ("时长 < 5 分钟",     dict(max_duration=300)),
        ("块数 > 1000",       dict(min_blocks=1000)),
        ("块数 < 10",         dict(max_blocks=10)),
        ("时长 5~15 分钟",     dict(min_duration=300, max_duration=900)),
        ("无任何条件",         dict()),
    ):
        got = form_filter(con, allu, **kw)
        print(f"    {label:<16} → {len(got):>4} 条"
              f"（{len(got)/len(allu)*100:.0f}%）")

    n_null = con.execute("SELECT COUNT(*) FROM item WHERE duration_sec IS NULL").fetchone()[0]
    print(f"\n  duration_sec 为 NULL 的条目: {n_null}")
    print(f"    它们不出现在「时长 < 5 分钟」里: "
          f"{format(form_filter(con, allu, max_duration=300) .__len__())} 条命中")
    con.close()

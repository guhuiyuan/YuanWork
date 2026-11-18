"""Re-read the files whose text lives in a text box or a picture.

Their chunks were cut before the anchors learned to say *where* - so
their citations had no position. Deleting them from the index and
rebuilding reads them again, with the new anchors.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765/api/v1"

#: The files whose text is in a text box or in a picture.
NAMES = (
    "课程讲义.docx",
    "<用户>.docx",
    "运算放大器与受控电源.docx",
    "电路元件的伏安特性测量.docx",
    "课程作业.docx",
    "实验报告.docx",
    "三相交流电路电压电流和功率的测量.docx",
    "叠加定理与戴维南定理验证.docx",
)


def call(path: str, method: str = "GET", payload: dict | None = None, timeout: float = 1800.0) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    wanted = set(NAMES)
    rows = call("/kb/files?limit=1000")["files"]
    targets = [r for r in rows if r["file_name"] in wanted]
    print(f"要重读的文件：{len(targets)} 个")
    for row in targets:
        print("  -", row["file_name"], row["chunk_count"], "个切片")

    for row in targets:
        call(f"/kb/files/{row['doc_id']}", method="DELETE")
    print("已从索引里删掉，等重建把它们读回来")

    job = call("/ingest/rebuild", method="POST", payload={})
    print("重建已启动：", job["job_id"])
    started = time.time()
    while True:
        status = call(f"/ingest/status/{job['job_id']}")
        if status["status"] in ("completed", "failed"):
            break
        if time.time() - started > 3600:
            print("等了一小时还没结束")
            return 1
        time.sleep(10.0)
    print(f"重建结束：{status['status']}，用时 {round(time.time() - started)} 秒")

    rows = call("/kb/files?limit=1000")["files"]
    for row in rows:
        if row["file_name"] in wanted:
            print(f"  · {row['file_name']}  {row['chunk_count']} 个切片")
    return 0


if __name__ == "__main__":
    sys.exit(main())

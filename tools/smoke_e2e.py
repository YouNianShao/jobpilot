r"""真机端到端自检：把"界面上点得到"的接口全部真跑一遍。

为什么需要它：单元测试全绿，但仪表盘的"评分"按钮依然可以是坏的
（`limit=None` 绑进 SQL 的 LIMIT 会抛 "datatype mismatch"，测试用例全部显式传了 limit，
恰好绕开了界面的真实路径）。这个脚本走的就是界面那条路 —— HTTP，不带可选参数。

它会造一个**合成岗位**跑完整生命周期，然后在结尾彻底删掉；你的真实岗位不受影响。
会真的调用模型（招呼语 / 定制简历 / 跟进话术各一次），因为这三条路径只有真调用才算验过。

用法: python tools\smoke_e2e.py [--base http://127.0.0.1:8700]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jobpilot.config import DB_PATH  # noqa: E402
from jobpilot.db import Repo, connect  # noqa: E402
from jobpilot.models import Job  # noqa: E402

BASE = "http://127.0.0.1:8700"
JOB_ID = "smoke-e2e-1"
RESULTS: list[tuple[bool, str, str]] = []
RUN_MARK = 0        # task_runs 的水位线：自检自己跑出来的记录，结束时删掉


def call(method: str, path: str, body: dict | None = None, timeout: int = 120):
    """返回 (status_code, 解析后的内容)。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            blob = resp.read()
            if resp.headers.get("Content-Type", "").startswith("application/zip"):
                return resp.status, blob          # 二进制（备份）原样返回
            raw = blob.decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (("  → " + detail) if detail else ""))


def wait_task(expected: str = "completed", limit: int = 90) -> dict:
    """等当前任务结束；返回任务快照。"""
    for _ in range(limit):
        time.sleep(1)
        _, status = call("GET", "/api/status")
        if not status.get("running"):
            return status.get("task") or {}
    return {"status": "timeout"}


def run_task(mode: str, platform: str = "boss") -> dict:
    code, body = call("POST", "/api/tasks", {"mode": mode, "platform": platform})
    if code != 200:
        return {"status": "rejected", "error": body.get("error") if isinstance(body, dict) else body}
    return wait_task()


def mark_run_watermark() -> None:
    """记下任务历史的水位线（只记在内存里，不在你的库里建任何表）。"""
    global RUN_MARK
    repo = Repo(connect(DB_PATH))
    try:
        row = repo.conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM task_runs").fetchone()
        RUN_MARK = int(row[0])
    finally:
        repo.conn.close()


def seed_synthetic_job() -> None:
    """造一个合成岗位（不进你的真实数据），并把它放到"待确认"。"""
    repo = Repo(connect(DB_PATH))
    try:
        repo.soft_delete([JOB_ID])                # 先清掉上次残留（purge 只删回收站里的）
        repo.purge([JOB_ID])
        repo.upsert_job(Job(
            id=JOB_ID, platform="boss", external_id="smoke", title="数据分析师（端到端自检）",
            company="自检用公司", city="上海", salary="20-30K",
            url="https://example.invalid/smoke",
            jd="负责经营分析、指标体系搭建与 A/B 实验，熟练 SQL 与 Python。",
            status="pending_confirm",
        ))
        repo.set_score(JOB_ID, 88, "端到端自检：构造数据", "pending_confirm")
    finally:
        repo.conn.close()


def backdate_applied(hours: float = 49) -> None:
    """把 applied 事件往前挪，好让"投递 48 小时后该跟进"这条规则命中。"""
    stamp = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    repo = Repo(connect(DB_PATH))
    try:
        repo.conn.execute("UPDATE events SET created_at = ? WHERE job_id = ? AND kind = 'applied'", (stamp, JOB_ID))
        repo.conn.commit()
    finally:
        repo.conn.close()


def cleanup() -> None:
    repo = Repo(connect(DB_PATH))
    try:
        # purge 只作用于回收站里的行（这是刻意的：界面上"彻底删除"只在回收站里出现），
        # 所以清理得先软删再彻底删 —— 这里踩过一次。
        repo.soft_delete([JOB_ID])
        repo.purge([JOB_ID])
        repo.conn.execute("DELETE FROM interactions WHERE job_id = ?", (JOB_ID,))
        repo.conn.commit()
    finally:
        repo.conn.close()
    # 自检跑出来的任务历史也清掉（只删水位线之后的）
    repo = Repo(connect(DB_PATH))
    try:
        if RUN_MARK:
            repo.conn.execute("DELETE FROM task_runs WHERE rowid > ?", (RUN_MARK,))
            repo.conn.commit()
        repo.conn.execute("DROP TABLE IF EXISTS _smoke_mark")   # 早先版本留下的表，顺手清掉
        repo.conn.commit()
    finally:
        repo.conn.close()
    # 合成岗位生成的定制简历文件也一起清掉，别在 data/resumes 里留垃圾
    for path in (ROOT / "data" / "resumes").glob(f"{JOB_ID}*"):
        try:
            path.unlink()
        except OSError:
            pass


def restart_via_shortcut(wait_seconds: int = 30) -> bool:
    """从**用户真实入口**（桌面快捷方式）重启看板。

    为什么要专门做这个：pytest / check_ui / smoke 全都是直连服务，
    于是"快捷方式启动链断了"这种故障对它们完全不可见 —— 而这个故障的
    表现是"双击没反应"，对用户等于整个项目挂了。启动链本身必须被验。
    """
    import subprocess

    ps = ("Get-CimInstance Win32_Process | Where-Object { "
          "$_.Name -like 'python*' -and $_.CommandLine -like '*jobpilot.cli*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    time.sleep(2)
    subprocess.Popen(["cmd", "/c", "start", "", str(ROOT / "启动 JobPilot.lnk")], shell=False)
    for _ in range(wait_seconds * 2):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=2):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def main() -> int:
    global BASE
    if "--base" in sys.argv:
        BASE = sys.argv[sys.argv.index("--base") + 1]

    print("JobPilot 真机端到端自检")
    print("=" * 56)

    if "--via-shortcut" in sys.argv:
        print("\n[入口] 通过桌面快捷方式重启看板（验证真实的启动链）")
        ok = restart_via_shortcut()
        check("快捷方式启动链可用", ok, "2 秒内响应" if ok else "30 秒内没起来")
        if not ok:
            print("\n启动链就断了，后面的检查没有意义。")
            return 1

    print("\n[0] 只读接口")
    code, status = call("GET", "/api/status")
    check("GET /api/status", code == 200 and "platforms" in status, f"HTTP {code}")
    code, health = call("GET", "/api/health")
    check("GET /api/health（浏览器连通性）", code == 200, str(health.get("ok")))
    code, cities = call("GET", "/api/cities?platform=boss")
    check("GET /api/cities（BOSS 城市表）", code == 200 and cities.get("count", 0) > 100, f"{cities.get('count')} 个城市")
    code, resume = call("GET", "/api/resume")
    check("GET /api/resume（简历可读）", code == 200 and resume.get("ok"), resume.get("problem") or f"{resume.get('chars')} 字")
    code, activity = call("GET", "/api/activity")
    check("GET /api/activity（记录页聚合）", code == 200 and "runs" in activity)
    code, cfg = call("GET", "/api/config")
    check("GET /api/config", code == 200 and "safety" in cfg)

    print("\n[0.1] 系统体检")
    code, report = call("GET", "/api/checks")
    check("GET /api/checks（体检）", code == 200 and report.get("checks"), f"ok={report.get('ok')} 注意={report.get('warned')}")
    check("必检项全过", report.get("ok") is True, str(report.get("failed")))

    print("\n[1] 能力门（不该跑的任务要被挡住，而不是跑完什么都没做）")
    # BOSS 的自动投递 2026-10-10 已实机核验并开放；能力门改用仍只有采集的智联来验。
    # 这里必须挑「能力不足」的平台：换成 boss 会真的把任务跑起来（会开真浏览器去投递）。
    code, body = call("POST", "/api/tasks", { "mode": "apply", "platform": "zhaopin" })
    check("未核验平台的 apply 被挡住 → 409", code == 409 and "自动投递" in str(body.get("error", "")), str(body.get("error", ""))[:40])
    code, body = call("POST", "/api/tasks", {"mode": "monitor", "platform": "boss"})
    check("monitor 未核验 → 409", code == 409 and "监测" in str(body.get("error", "")), str(body.get("error"))[:40])

    print("\n[2] 合成岗位 + 招呼语（真实模型调用）")
    mark_run_watermark()
    seed_synthetic_job()
    task = run_task("greet")
    repo = Repo(connect(DB_PATH))
    greeting = (repo.get_job(JOB_ID).greeting or "")
    repo.conn.close()
    check("POST /api/tasks greet 完成", task.get("status") == "completed", str(task.get("error") or ""))
    check("招呼语真的生成了", len(greeting) > 10, greeting[:44])

    print("\n[3] 人工确认门")
    code, body = call("POST", "/api/jobs/confirm", {"platform": "boss", "ids": [JOB_ID]})
    check("POST /api/jobs/confirm", code == 200 and body.get("confirmed") == 1, str(body))

    print("\n[4] 手动投递回执（与自动投递同一套计数）")
    code, body = call("POST", f"/api/jobs/{JOB_ID}/applied", {"note": "端到端自检"})
    check("POST /api/jobs/{id}/applied", code == 200 and body.get("ok"), str(body)[:70])
    code, again = call("POST", f"/api/jobs/{JOB_ID}/applied", {})
    check("重复标记被挡住", code == 409 and "已经记过" in str(again.get("error")), str(again.get("error"))[:40])

    print("\n[4.1] 投递之后的进展（HR 回复 / 约面）")
    code, body = call("POST", f"/api/jobs/{JOB_ID}/progress", {"state": "interview", "note": "约了周三下午"})
    check("POST /api/jobs/{id}/progress", code == 200 and body.get("label") == "约面试", str(body)[:60])
    code, rows = call("GET", "/api/interactions")
    check("监测页能看到这条（且带岗位标题）",
          code == 200 and any(r.get("job_id") == JOB_ID and r.get("title") for r in rows))
    code, activity = call("GET", "/api/activity")
    check("漏斗里出现面试计数", code == 200 and activity.get("funnel", {}).get("interview", 0) >= 1)

    print("\n[4.2] 面试准备（真实模型调用，针对岗位 JD）")
    code, prep = call("POST", f"/api/jobs/{JOB_ID}/interview-prep", {})
    check("POST /api/jobs/{id}/interview-prep", code == 200 and prep.get("questions"), str(prep.get("error"))[:60])
    if code == 200 and prep.get("questions"):
        first = prep["questions"][0]
        print("     示例问题：" + first["q"][:60])
        print("     该讲哪段：" + (first.get("angle") or "")[:60])
    check("提示词版本被记录", str(prep.get("prompt", "")).startswith("interview.v1.md"))

    print("\n[5] 跟进话术（真实模型调用）")
    backdate_applied()
    code, due = call("GET", "/api/follow-ups")
    check("GET /api/follow-ups 里出现该岗位", code == 200 and any(r["id"] == JOB_ID for r in due))
    task = run_task("followup")
    repo = Repo(connect(DB_PATH))
    follow = (repo.get_job(JOB_ID).follow_up or "")
    repo.conn.close()
    check("跟进话术真的生成了", len(follow) > 5, follow[:44])

    print("\n[6] 定制简历（真实模型调用）")
    code, body = call("POST", f"/api/jobs/{JOB_ID}/resume", {})
    check("POST /api/jobs/{id}/resume", code == 200 and not body.get("error"), str(body.get("error") or body.get("path"))[:70])
    check("简历文件落盘了", bool(body.get("path")) and Path(str(body.get("path"))).exists())

    print("\n[7] 导出")
    code, text = call("GET", "/api/export")
    check("GET /api/export 含该岗位", code == 200 and JOB_ID in str(text))

    print("\n[7.1] 备份（整条流水线打包）")
    code, blob = call("GET", "/api/backup", timeout=60)
    is_zip = isinstance(blob, bytes) and blob[:2] == b"PK"
    check("GET /api/backup 返回合法 zip", code == 200 and is_zip, f"{code} · {len(blob) if isinstance(blob, bytes) else 'n/a'} 字节")
    if is_zip:
        import io as _io
        import zipfile as _zip
        names = _zip.ZipFile(_io.BytesIO(blob)).namelist()
        check("备份里含数据库与 manifest", "data/jobpilot.db" in names and "manifest.json" in names, "、".join(names))

    print("\n[8] 回收站：删除 → 恢复 → 彻底删除")
    code, body = call("POST", "/api/jobs/delete", {"ids": [JOB_ID]})
    check("移入回收站", code == 200 and body.get("deleted") == 1, str(body))
    code, deleted = call("GET", "/api/jobs?deleted=1&limit=50")
    check("回收站里能看到它", code == 200 and any(j["id"] == JOB_ID for j in deleted))
    code, body = call("POST", "/api/jobs/restore", {"ids": [JOB_ID]})
    check("恢复", code == 200 and body.get("restored") == 1, str(body))

    print("\n[9] 暂停 / 继续 / 停止（空闲时应当无害）")
    check("POST /api/tasks/pause", call("POST", "/api/tasks/pause", {})[0] == 200)
    check("POST /api/tasks/resume", call("POST", "/api/tasks/resume", {})[0] == 200)
    check("POST /api/tasks/stop", call("POST", "/api/tasks/stop", {})[0] == 200)

    print("\n[10] 清理合成数据")
    cleanup()
    repo = Repo(connect(DB_PATH))
    gone = repo.get_job(JOB_ID) is None
    repo.conn.close()
    check("合成岗位已彻底删除", gone)

    failed = [name for ok, name, _ in RESULTS if not ok]
    print("\n" + "=" * 56)
    print(f"通过 {len(RESULTS) - len(failed)}/{len(RESULTS)}")
    if failed:
        print("失败项：")
        for name in failed:
            print("  -", name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

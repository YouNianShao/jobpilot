"""求职进展：投递之后发生了什么（HR 回复 → 约面 → offer）。

为什么需要它：工具原来到"已投递"就断了，而求职的后半程全在聊天里 ——
谁看过了、谁约了面试、面试排到哪天、谁给了 offer。这些现在只能记在脑子里，
于是"监测"页永远是空的、漏斗也只能画到投递。

不碰浏览器：这里是人工「记录进展」。自动的那一半由监测驱动写同一张 interactions 表
（BOSS 只读聊天列表；前程无忧 / 猎聘 / 智联只读「我的投递」），人工记录用自己的 key，互不覆盖。
"""

from __future__ import annotations

from .context import ServiceContext

# key -> 界面显示的中文名。key 存库，中文只在界面用（和 nature / 城市表一个套路）。
PROGRESS_STATES: dict[str, str] = {
    "viewed": "已查看",
    "replied": "已回复",
    "interview": "约面试",
    "interviewed": "面试完",
    "offer": "拿到 offer",
    "rejected": "被拒",
}

# 漏斗里"进入面试"要算上已经面完和拿到 offer 的
INTERVIEW_STATES = ("interview", "interviewed", "offer")


def state_labels() -> dict[str, str]:
    return dict(PROGRESS_STATES)


def record_progress(ctx: ServiceContext, job_id: str, state: str, *, note: str = "") -> dict:
    key = str(state or "").strip()
    job = ctx.repo.get_job(job_id)
    if job is None:
        return {"error": f"没有这个岗位：{job_id}"}
    if key not in PROGRESS_STATES:
        return {"error": f"未知的进展状态：{state}（可用：{'、'.join(PROGRESS_STATES)}）"}

    label = PROGRESS_STATES[key]
    text = (note or "").strip()
    ctx.repo.upsert_interaction(
        key=f"manual:{job_id}", platform=job.platform, job_id=job_id,
        hr_name="", state=key, last_text=text, deeplink=job.url or "",
    )
    ctx.repo.add_event(job_id, "progress", f"{label}{(' · ' + text) if text else ''}")
    return {"ok": True, "job_id": job_id, "state": key, "label": label, "title": job.title}

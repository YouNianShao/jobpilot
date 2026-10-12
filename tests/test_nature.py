"""岗位性质识别：外包/培训/营销话术的标记与预筛。"""
from __future__ import annotations

from jobpilot.config import AppConfig
from jobpilot.models import Job
from jobpilot.nature import describe, detect, nature_labels
from jobpilot.prefilter import prefilter


def make_job(**kw) -> Job:
    base = dict(id="boss-1", platform="boss", external_id="1", title="数据分析师", company="某某科技")
    base.update(kw)
    return Job(**base)


def test_detects_real_noise_titles():
    """用真实采集回来的标题试：噪音要能认出来，正常岗位不能误伤。"""
    assert detect("AI豆包人工智能回复新风口！提供单人间！", "聚霖财") == ("marketing",)
    assert detect("AI Agent人工智能大客户销售 完整带教体系", "珍岛集团", "无责底薪 高提成") == ("marketing",)
    assert detect("资深数据分析师", "柯莱特集团", "SQL/Python 建模，A/B 实验") == ()
    assert detect("数据分析师（驻场）", "某人力资源有限公司", "外包岗，甲方现场办公") == ("outsourcing",)
    assert detect("Java开发（零基础可培养）", "某科技", "先培训后上岗") == ("training",)


def test_labels_and_describe():
    labels = nature_labels()
    assert set(labels) == {"outsourcing", "training", "marketing"}
    assert describe(["outsourcing", "training"]) == "外包/派遣、培训/招生"


def test_prefilter_blocks_only_when_configured():
    config = AppConfig()
    noisy = make_job(title="AI Agent人工智能大客户销售", company="珍岛集团", jd="无责底薪 高提成")

    # 默认只标记不剔除 —— 不替用户做决定
    assert prefilter(noisy, config) == (False, "")

    config.profile.block_natures = ["marketing"]
    rejected, reason = prefilter(noisy, config)
    assert rejected is True
    assert "营销话术" in reason

    # 正常岗位即使开了阻断也不受影响
    clean = make_job(title="资深数据分析师", company="柯莱特集团", jd="SQL 建模")
    assert prefilter(clean, config) == (False, "")

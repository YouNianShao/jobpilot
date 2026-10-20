"""重复岗位：同一平台、同标题、同公司、同城市的岗位只进库一次（id 变了也一样）。"""
from __future__ import annotations

from jobpilot.models import Job


def _job(i, **kw):
    base = dict(id=f"zhaopin-{i}", platform="zhaopin", external_id=str(i), title="AI数据标注",
                company="某信息技术有限公司", city="上海", salary="1.2-1.8万", url="")
    base.update(kw)
    return Job(**base)


def test_same_job_with_a_new_id_is_not_inserted_again(repo):
    """2026-10-15 智联：列表没带编号时 id 用「标题|公司|薪资」哈希，没登录时薪资是「**-**元」→ 同岗位两个 id。"""
    assert repo.upsert_job(_job("hashA", salary="**-**元"))[0] is True
    inserted, existing = repo.upsert_job(_job("hashB", salary="1.2-1.8万", url="https://www.zhaopin.com/jobdetail/X.htm"))
    assert inserted is False and existing == "zhaopin-hashA"
    kept = repo.get_job("zhaopin-hashA")
    assert kept.salary == "1.2-1.8万" and kept.url.endswith("X.htm")           # 打码的薪资、空链接顺手补上
    assert repo.get_job("zhaopin-hashB") is None


def test_same_title_in_another_city_or_company_is_a_different_job(repo):
    assert repo.upsert_job(_job("a"))[0] is True
    assert repo.upsert_job(_job("b", city="北京"))[0] is True
    assert repo.upsert_job(_job("c", company="另一家公司"))[0] is True


def test_rerunning_collection_does_not_duplicate(repo):
    """同一个岗位（同一个 id）采三遍，库里还是一条。"""
    for _ in range(3):
        for i in range(5):
            repo.upsert_job(_job(f"n{i}", title=f"岗位{i}"))
    assert len(repo.list_jobs(platform="zhaopin", limit=100)) == 5


def test_two_real_postings_with_the_same_title_are_both_kept(repo):
    """同一家公司同一个标题挂两个职位（各有平台编号和链接）：是两个岗位，不能合并。"""
    assert repo.upsert_job(_job("111", url="https://www.zhaopin.com/jobdetail/111.htm"))[0] is True
    assert repo.upsert_job(_job("222", url="https://www.zhaopin.com/jobdetail/222.htm"))[0] is True


def test_recollected_job_fills_the_link_of_an_old_linkless_record(repo):
    """智联以前滚动加载的岗位没有链接（哈希 id）；修好后重新采到同一个岗位，应补上链接而不是再建一条。"""
    assert repo.upsert_job(_job("hash1", salary="1-2万", url=""))[0] is True
    inserted, existing = repo.upsert_job(_job("CC123J456", salary="1-2万", url="https://www.zhaopin.com/jobdetail/CC123J456.htm"))
    assert inserted is False and existing == "zhaopin-hash1"
    assert repo.get_job("zhaopin-hash1").url.endswith("CC123J456.htm")

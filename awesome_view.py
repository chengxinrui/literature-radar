#!/usr/bin/env python3
"""Render a curated, Awesome-style Markdown view from the raw literature database.

The raw JSON remains the complete candidate store. The human-readable Markdown
only shows recognized CCF-A / configured field-top venues plus a separate,
clearly marked preprint watchlist.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import literature_radar as lr


STRICT_FULL_VENUE_ALIASES = {
    "artificial intelligence",
    "automatica",
    "mathematical programming",
    "operations research",
    "management science",
    "transportation science",
    "nature machine intelligence",
    "nature computational science",
    "ieee transactions on automatic control",
    "siam journal on control and optimization",
    "siam journal on scientific computing",
    "siam journal on numerical analysis",
    "journal of computational physics",
    "computer methods in applied mechanics and engineering",
    "siam review",
    "informs journal on computing",
}


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", lr.clean_text(value).casefold())


def aliases(entry: Any) -> list[str]:
    if isinstance(entry, str):
        return [entry]
    if isinstance(entry, dict):
        result = [lr.clean_text(entry.get("name", ""))]
        result.extend(lr.clean_text(x) for x in entry.get("aliases", []) if lr.clean_text(x))
        return [x for x in result if x]
    return []


def alias_matches(venue: str, alias: str) -> bool:
    venue_norm = norm(venue)
    alias_norm = norm(alias)
    if not venue_norm or not alias_norm:
        return False
    # Match short acronyms as whole tokens to avoid e.g. matching "AI" inside a title.
    if alias.isupper() and len(alias) <= 6:
        return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", venue, re.I))
    strict_names = {norm(x) for x in STRICT_FULL_VENUE_ALIASES}
    if alias_norm in strict_names:
        return venue_norm == alias_norm
    return len(alias_norm) >= 5 and alias_norm in venue_norm


def all_venue_texts(paper: dict[str, Any]) -> list[str]:
    values = [lr.clean_text(paper.get("venue", ""))]
    for signal in paper.get("status_signals", []) or []:
        if isinstance(signal, dict):
            values.append(lr.clean_text(signal.get("venue", "")))
            values.append(lr.clean_text(signal.get("journal_ref", "")))
    return lr.unique_strings([x for x in values if x])


def entry_matches(venue_texts: list[str], entries: list[Any]) -> str:
    for entry in entries or []:
        name = lr.clean_text(entry.get("name", "")) if isinstance(entry, dict) else lr.clean_text(entry)
        for alias in aliases(entry):
            if any(alias_matches(text, alias) for text in venue_texts):
                return name or alias
    return ""


def is_preprint(paper: dict[str, Any]) -> bool:
    status = str(paper.get("publication_status", ""))
    # A record already marked as published/accepted is not re-labelled as a preprint
    # merely because an arXiv version also exists.
    if status.startswith(("已发表/", "已接收/")):
        return False
    if paper.get("arxiv_id"):
        return True
    for signal in paper.get("status_signals", []) or []:
        if not isinstance(signal, dict):
            continue
        venue = lr.clean_text(signal.get("venue", ""))
        work_type = lr.clean_text(signal.get("work_type", "")).casefold()
        if lr.PREPRINT_RE.search(venue) or work_type in ("preprint", "posted-content"):
            return True
    return status.startswith("arXiv/")


def classify_venue(paper: dict[str, Any], policy: dict[str, Any]) -> tuple[str, str]:
    venue_texts = all_venue_texts(paper)
    ccf_bc = entry_matches(venue_texts, policy.get("ccf_bc_venues", []))
    if ccf_bc:
        return "exclude", f"CCF-B/C：{ccf_bc}"
    ccf_a = entry_matches(venue_texts, policy.get("ccf_a_venues", []))
    if ccf_a:
        return "ccf_a", f"CCF-A：{ccf_a}"
    field_top = entry_matches(venue_texts, policy.get("field_top_venues", []))
    if field_top:
        return "field_top", f"领域强 venue：{field_top}"
    if is_preprint(paper) and policy.get("include_preprints", True):
        return "preprint", "预印本候选（尚未由正式 venue 验证）"
    return "exclude", "venue 未进入当前白名单"


def institution_matches(paper: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    institution_texts: list[str] = []
    for inst in paper.get("institutions", []) or []:
        if isinstance(inst, dict) and inst.get("name"):
            institution_texts.append(lr.clean_text(inst["name"]))
    for affiliation in paper.get("affiliations", []) or []:
        if isinstance(affiliation, dict) and affiliation.get("institution"):
            institution_texts.append(lr.clean_text(affiliation["institution"]))
    results = []
    for configured in policy.get("priority_institutions", []) or []:
        configured_norm = norm(configured)
        if len(configured_norm) < 5:
            continue
        if any(configured_norm in norm(value) for value in institution_texts if norm(value)):
            results.append(lr.clean_text(configured))
    return lr.unique_strings(results)


def event_date(paper: dict[str, Any], today: date) -> date | None:
    publication = lr.parse_date(paper.get("publication_date"))
    preprint = lr.parse_date(paper.get("preprint_date"))
    # Prefer the formal publication date when it is already known. If its venue date
    # is still in the future, use the date the preprint first became available.
    if publication and date.fromisoformat(publication) <= today:
        return date.fromisoformat(publication)
    if preprint and date.fromisoformat(preprint) <= today:
        return date.fromisoformat(preprint)
    if publication:
        return date.fromisoformat(publication)
    if preprint:
        return date.fromisoformat(preprint)
    first_seen = lr.parse_date(paper.get("first_seen"))
    return date.fromisoformat(first_seen) if first_seen else None


def month_for(paper: dict[str, Any], today: date) -> str:
    value = event_date(paper, today)
    return value.strftime("%Y-%m") if value else "日期未知"


def citation_reference_date(paper: dict[str, Any], today: date) -> date | None:
    # Citation velocity should use the paper's earliest known public date (usually
    # its preprint date), not first_seen, which can be today during a backfill.
    dates = []
    for field in ("preprint_date", "publication_date"):
        parsed = lr.parse_date(paper.get(field))
        if parsed:
            candidate = date.fromisoformat(parsed)
            if candidate <= today:
                dates.append(candidate)
    if dates:
        return min(dates)
    first_seen = lr.parse_date(paper.get("first_seen"))
    if first_seen:
        return date.fromisoformat(first_seen)
    return event_date(paper, today)


def primary_category(paper: dict[str, Any], topics: list[dict[str, Any]]) -> tuple[str, list[str]]:
    order = {lr.clean_text(t.get("name", "")): i for i, t in enumerate(topics) if isinstance(t, dict)}
    scores: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for match in paper.get("topic_matches", []) or []:
        if not isinstance(match, dict):
            continue
        name = lr.clean_text(match.get("topic", ""))
        if name:
            try:
                scores[name] += float(match.get("weight", 1.0))
            except (TypeError, ValueError):
                scores[name] += 1.0
            counts[name] += 1
    candidates = lr.unique_strings((paper.get("topics") or []) + list(scores.keys()))
    if not candidates:
        return "其他 / 待分类", []
    primary = max(candidates, key=lambda name: (scores.get(name, 0.0), counts.get(name, 0), -order.get(name, 999)))
    return primary, candidates


def citation_stats(paper: dict[str, Any], today: date) -> tuple[int, float]:
    try:
        count = max(0, int(paper.get("citation_count") or 0))
    except (TypeError, ValueError):
        count = 0
    reference_date = citation_reference_date(paper, today)
    age_months = max(1.0, ((today - reference_date).days / 30.4375) if reference_date else 1.0)
    return count, count / age_months


def annotate(paper: dict[str, Any], topics: list[dict[str, Any]], policy: dict[str, Any], today: date) -> dict[str, Any]:
    copy = dict(paper)
    bucket, label = classify_venue(paper, policy)
    copy["_bucket"] = bucket
    copy["_venue_label"] = label
    copy["_institution_matches"] = institution_matches(paper, policy)
    copy["_citation_count"], copy["_citations_per_month"] = citation_stats(paper, today)
    copy["_citation_data_available"] = paper.get("citation_count") is not None
    copy["_month"] = month_for(paper, today)
    copy["_primary_category"], copy["_all_categories"] = primary_category(paper, topics)
    effective_date = event_date(paper, today)
    copy["_event_date"] = effective_date.isoformat() if effective_date else ""
    copy["_event_ordinal"] = effective_date.toordinal() if effective_date else 0
    return copy


def within_window(paper: dict[str, Any], cutoff: date, today: date) -> bool:
    candidates = []
    for field in ("publication_date", "preprint_date"):
        parsed = lr.parse_date(paper.get(field))
        if parsed:
            candidates.append(date.fromisoformat(parsed))
    if not candidates:
        parsed = lr.parse_date(paper.get("first_seen"))
        if parsed:
            candidates.append(date.fromisoformat(parsed))
    return any(cutoff <= item <= today for item in candidates)


def sort_key(paper: dict[str, Any], preprint: bool = False) -> tuple[Any, ...]:
    institution = bool(paper.get("_institution_matches"))
    recent_first = -paper.get("_event_ordinal", 0)
    if preprint:
        return (-int(institution), -paper.get("_citations_per_month", 0.0), -paper.get("_citation_count", 0), recent_first)
    bucket_rank = {"ccf_a": 2, "field_top": 1}.get(paper.get("_bucket"), 0)
    return (-paper.get("_citation_count", 0), -paper.get("_citations_per_month", 0.0), -int(institution), -bucket_rank, recent_first)


def markdown_card(paper: dict[str, Any], index: int) -> list[str]:
    title = lr.clean_text(paper.get("title", "")) or "无标题"
    link = lr.clean_text(paper.get("paper_url", ""))
    title_link = f"[{title}]({link})" if link else title
    authors = "、".join((paper.get("authors") or [])[:8])
    if len(paper.get("authors") or []) > 8:
        authors += " 等"
    institution_parts = []
    for inst in paper.get("institutions", []) or []:
        if isinstance(inst, dict) and inst.get("name"):
            country = lr.country_label(inst.get("country_code", ""))
            institution_parts.append(f"{inst['name']} ({country})" if country else inst["name"])
    institutions = lr.unique_strings(institution_parts)
    abstract = lr.clean_text(paper.get("abstract", ""))
    if len(abstract) > 420:
        abstract = abstract[:420].rstrip() + "…"
    citation_count = paper.get("_citation_count", 0)
    citation_rate = paper.get("_citations_per_month", 0.0)
    citation_text = f"{citation_count}（约 {citation_rate:.1f}/月，OpenAlex）" if paper.get("_citation_data_available", paper.get("citation_count") is not None) else "暂无数据（OpenAlex 未提供/未查询）"
    inst_matches = paper.get("_institution_matches") or []
    inst_signal = "；优先机构信号：" + "、".join(inst_matches[:3]) if inst_matches else ""
    category_tags = "、".join(paper.get("_all_categories") or [])
    metadata_sources = "、".join(lr.unique_strings(paper.get("source_names") or [])) or "未知"
    status_evidence = "；".join(lr.unique_strings(paper.get("status_evidence") or []))
    github_search = f"https://github.com/search?q={quote(title, safe='')}&type=repositories"
    lines = [
        f"#### {index}. {title_link}",
        f"- **质量/状态：**{paper.get('_venue_label', '未分级')}；{paper.get('publication_status', '状态未知')}",
        f"- **状态依据：**{status_evidence or '当前来源未提供明确依据，建议人工核验'}",
        f"- **日期：**发表 {paper.get('publication_date') or '—'}；预印本首发 {paper.get('preprint_date') or '—'}；收录月份 {paper.get('_month', '未知')}",
        f"- **类别：**{category_tags or '未分类'}",
        f"- **引用：**{citation_text}{inst_signal}",
        f"- **venue：**{paper.get('venue') or '—'}　**作者：**{authors or '未提供'}",
        f"- **机构：**{'；'.join(institutions) or '未提供/未识别'}",
        f"- **元数据来源：**{metadata_sources}",
        f"- **代码线索：**[在 GitHub 搜索项目名]({github_search})（搜索结果需自行核对，非已确认官方仓库）",
    ]
    if paper.get("doi"):
        lines.append(f"- **DOI：**[{paper['doi']}](https://doi.org/{paper['doi']})")
    if paper.get("arxiv_id"):
        lines.append(f"- **arXiv：**[摘要页](https://arxiv.org/abs/{paper['arxiv_id']})　[PDF](https://arxiv.org/pdf/{paper['arxiv_id']})")
    elif paper.get("open_access_url"):
        lines.append(f"- **开放获取：**[查看链接]({paper['open_access_url']})")
    if abstract:
        lines.append(f"- **摘要：**{abstract}")
    return lines + [""]


def render_awesome(papers: list[dict[str, Any]], topics: list[dict[str, Any]], policy: dict[str, Any], today: date, cutoff: date) -> str:
    annotated = [annotate(paper, topics, policy, today) for paper in papers if within_window(paper, cutoff, today)]
    curated = [paper for paper in annotated if paper["_bucket"] in ("ccf_a", "field_top", "preprint")]
    months: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for paper in curated:
        months[paper["_month"]][paper["_primary_category"]].append(paper)

    start_month = cutoff.strftime("%Y-%m")
    end_month = today.strftime("%Y-%m")
    lines = [
        f"# Awesome 文献雷达｜{start_month} 至 {end_month}",
        "",
        f"更新日期：{today.isoformat()}（Asia/Shanghai）。滚动窗口：最近 {max(1, (today - cutoff).days)} 天。",
        f"共收录精选论文/预印本 {len(curated)} 篇；其中正式 venue 精选 {sum(p['_bucket'] != 'preprint' for p in curated)} 篇，预印本观察 {sum(p['_bucket'] == 'preprint' for p in curated)} 篇。",
        "> 正式发表区仅显示 CCF-A 或配置中的跨领域强 venue；已知 CCF-B/C 和未进入白名单的正式 venue 不进入本 Markdown。预印本单列且不等于质量已验证。原始候选仍保存在 `papers.json`。",
        "> 同一月份/类别内按引用量优先，并显示按论文年龄折算的月均引用；预印本优先展示机构信号，再参考引用与日期。引用量会滞后，不能单独代表质量。",
        "",
    ]
    if not curated:
        lines.append("当前窗口没有满足精选规则的论文。可以检查 `quality_policy` venue 列表，或稍后增加领域 venue 白名单。")
        return "\n".join(lines) + "\n"

    for month in sorted(months.keys(), reverse=True):
        lines.extend([f"## {month}", ""])
        categories = sorted(months[month].keys())
        for category in categories:
            entries = months[month][category]
            published = [p for p in entries if p["_bucket"] in ("ccf_a", "field_top")]
            preprints = [p for p in entries if p["_bucket"] == "preprint"]
            lines.extend([f"### {category}", ""])
            if published:
                lines.extend(["#### 高质量正式 venue", ""])
                for i, paper in enumerate(sorted(published, key=lambda p: sort_key(p)), 1):
                    lines.extend(markdown_card(paper, i))
            if preprints:
                lines.extend(["#### 前沿预印本｜未经过正式 venue 验证", ""])
                for i, paper in enumerate(sorted(preprints, key=lambda p: sort_key(p, preprint=True)), 1):
                    lines.extend(markdown_card(paper, i))
    return "\n".join(lines).rstrip() + "\n"


def make_daily_markdown(papers: list[dict[str, Any]], topics: list[dict[str, Any]], policy: dict[str, Any], today: date, cutoff: date) -> str:
    day_papers = [p for p in papers if lr.parse_date(p.get("first_seen")) == today.isoformat() and within_window(p, cutoff, today)]
    annotated = [annotate(p, topics, policy, today) for p in day_papers]
    visible = [p for p in annotated if p["_bucket"] in ("ccf_a", "field_top", "preprint")]
    visible.sort(key=lambda p: sort_key(p, preprint=p["_bucket"] == "preprint"))
    title = f"# {today.isoformat()}｜本轮首次收录的精选文献 {len(visible)} 篇"
    note = "本文件按本轮首次入库日期汇总；首次六个月回溯可能集中录入历史论文，请按论文发表/预印本日期理解，不代表它们都在今天发表。"
    lines = [title, "", f"查询窗口起点：{cutoff.isoformat()}。", f"> {note}", ""]
    for i, paper in enumerate(visible, 1):
        lines.extend(markdown_card(paper, i))
    if not visible:
        lines.append("本轮没有新收录符合精选规则的条目。")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a month/category-organized Awesome-style literature index")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    config_path = Path(args.config)
    config = lr.load_config(config_path)
    config_dir = config_path.resolve().parent
    output_dir = Path(config.get("output_dir", "data"))
    if not output_dir.is_absolute():
        output_dir = config_dir / output_dir
    papers = lr.load_existing(output_dir / "papers.json")
    today = lr.today_local()
    lookback_days = max(1, int(config.get("lookback_days", 183)))
    cutoff = today - timedelta(days=lookback_days)
    topics = config.get("topics", []) or []
    policy = config.get("quality_policy", {}) or {}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "papers.md").write_text(render_awesome(papers, topics, policy, today, cutoff), encoding="utf-8")
    daily_content = make_daily_markdown(papers, topics, policy, today, cutoff)
    if any(lr.parse_date(p.get("first_seen")) == today.isoformat() and within_window(p, cutoff, today) for p in papers):
        daily_dir = output_dir / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        (daily_dir / f"{today.isoformat()}.md").write_text(daily_content, encoding="utf-8")
    (output_dir / "new_this_run.md").write_text(daily_content, encoding="utf-8")
    print(f"Awesome-style Markdown updated: {output_dir / 'papers.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

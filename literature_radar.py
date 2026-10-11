#!/usr/bin/env python3
"""A small, configurable literature radar: arXiv + Crossref + OpenAlex.

The program stores bibliographic metadata, abstracts, affiliation clues and links.
It does not download full-text PDFs. Publication/acceptance status is deliberately
conservative because those states are not consistently represented in metadata.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import requests
import yaml

APP_NAME = "LiteratureRadar/0.1"
ARXIV_API = "https://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"
ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"
NS = {"atom": ATOM_NS, "arxiv": ARXIV_NS}

COUNTRY_NAMES = {
    "CN": "中国", "US": "美国", "GB": "英国", "DE": "德国", "FR": "法国",
    "JP": "日本", "KR": "韩国", "IN": "印度", "CA": "加拿大", "AU": "澳大利亚",
    "SG": "新加坡", "NL": "荷兰", "CH": "瑞士", "IT": "意大利", "ES": "西班牙",
    "SE": "瑞典", "NO": "挪威", "DK": "丹麦", "FI": "芬兰", "BE": "比利时",
    "AT": "奥地利", "IE": "爱尔兰", "HK": "中国香港", "MO": "中国澳门",
    "TW": "中国台湾", "BR": "巴西", "IL": "以色列", "RU": "俄罗斯",
}

COUNTRY_PATTERNS = [
    (r"中国|people'?s republic of china|prc|p\.r\. china|\bchina\b", "CN"),
    (r"united states of america|united states|\bu\.s\.a?\.?\b|\busa\b", "US"),
    (r"united kingdom|great britain|\buk\b", "GB"),
    (r"\bgermany\b|\bdeutschland\b", "DE"),
    (r"\bfrance\b", "FR"), (r"\bjapan\b", "JP"),
    (r"south korea|republic of korea|\bkorea\b", "KR"), (r"\bindia\b", "IN"),
    (r"\bcanada\b", "CA"), (r"\baustralia\b", "AU"), (r"\bsingapore\b", "SG"),
    (r"\bnetherlands\b|\bholland\b", "NL"), (r"\bswitzerland\b", "CH"),
    (r"\bitaly\b", "IT"), (r"\bspain\b", "ES"), (r"\bsweden\b", "SE"),
    (r"\bnorway\b", "NO"), (r"\bdenmark\b", "DK"), (r"\bfinland\b", "FI"),
    (r"\bbelgium\b", "BE"), (r"\baustria\b", "AT"), (r"\bireland\b", "IE"),
    (r"hong kong", "HK"), (r"macau|macao", "MO"), (r"taiwan", "TW"),
    (r"\bbrazil\b", "BR"), (r"\bisrael\b", "IL"), (r"\brussia\b", "RU"),
]

PREPRINT_RE = re.compile(
    r"arxiv|bio\s?rxiv|med\s?rxiv|chemrxiv|ssrn|preprint|research square|osf preprints?",
    re.IGNORECASE,
)
ACCEPTED_RE = re.compile(
    r"\b(accepted|in press|to appear|forthcoming|accepted for publication)\b",
    re.IGNORECASE,
)
NOT_PUBLISHED_RE = re.compile(
    r"\b(submitted|under review|under consideration|not published|to be submitted)\b",
    re.IGNORECASE,
)


def today_local() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(str(x) for x in value if x is not None)
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_date(value: Any) -> str:
    """Normalize common API date representations to YYYY-MM-DD."""
    if value is None:
        return ""
    if isinstance(value, dict):
        parts = value.get("date-parts") or value.get("date_parts") or []
        if parts and isinstance(parts[0], (list, tuple)) and parts[0]:
            try:
                year = int(parts[0][0])
                month = int(parts[0][1]) if len(parts[0]) > 1 else 1
                day = int(parts[0][2]) if len(parts[0]) > 2 else 1
                return date(year, month, day).isoformat()
            except (TypeError, ValueError):
                return ""
        return ""
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], (list, tuple)):
            return parse_date({"date-parts": value})
        return ""
    text = str(value).strip()
    match = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", text)
    if not match:
        return ""
    try:
        y = int(match.group(1))
        m = int(match.group(2) or 1)
        d = int(match.group(3) or 1)
        return date(y, m, d).isoformat()
    except ValueError:
        return ""


def normalize_doi(value: Any) -> str:
    text = clean_text(value).strip().lower()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi:\s*", "", text)
    return text.strip().rstrip(".,;)")


def is_arxiv_doi(doi: str) -> bool:
    return bool(re.match(r"^10\.48550/arxiv\.", (doi or "").lower()))


def extract_arxiv_id(value: Any) -> str:
    text = clean_text(value)
    match = re.search(r"(?:arxiv\.org/(?:abs|pdf)/)?((?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?)", text, re.I)
    if not match:
        return ""
    return re.sub(r"v\d+$", "", match.group(1), flags=re.I)


def normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean_text(value)).casefold()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def country_from_text(value: Any) -> str:
    text = clean_text(value)
    for pattern, code in COUNTRY_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return code
    return ""


def country_label(code: str) -> str:
    code = (code or "").upper()
    if not code:
        return ""
    return f"{COUNTRY_NAMES.get(code, code)} ({code})"


def reconstruct_abstract(inverted_index: Any) -> str:
    if not isinstance(inverted_index, dict) or not inverted_index:
        return ""
    positions: dict[int, str] = {}
    for token, indexes in inverted_index.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            try:
                positions[int(index)] = str(token)
            except (TypeError, ValueError):
                continue
    if not positions:
        return ""
    return " ".join(positions[i] for i in sorted(positions))


def new_record() -> dict[str, Any]:
    return {
        "uid": "",
        "title": "",
        "abstract": "",
        "authors": [],
        "institutions": [],
        "affiliations": [],
        "institution_countries": [],
        "home_scope": "未知",
        "venue": "",
        "publication_status": "未知（元数据不足）",
        "status_evidence": [],
        "peer_review_status": "需人工核验",
        "publication_date": "",
        "preprint_date": "",
        "preprint_updated_date": "",
        "arxiv_id": "",
        "doi": "",
        "work_type": "",
        "citation_count": None,
        "topics": [],
        "search_topics": [],
        "relevance_status": "",
        "relevance_anchors": [],
        "relevance_exclusion_reasons": [],
        "categories": [],
        "topic_matches": [],
        "source_names": [],
        "source_urls": [],
        "paper_url": "",
        "open_access_url": "",
        "is_open_access": None,
        "status_signals": [],
        "first_seen": today_local().isoformat(),
        "last_status_checked": "",
        "reading_status": "未读",
        "my_notes": "",
    }


def topic_match(topic: dict[str, Any], query_term: str) -> dict[str, Any]:
    return {
        "topic": str(topic.get("name") or "未分类"),
        "role": str(topic.get("role") or "adjacent"),
        "weight": float(topic.get("weight", 1.0)),
        "query_term": str(query_term),
    }


def relevance_phrase_in_text(text: str, phrase: str) -> bool:
    if phrase.isupper() and len(phrase) <= 4:
        return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"s?(?![A-Za-z0-9])", text, re.I))
    phrase_norm = normalize_title(phrase)
    return bool(phrase_norm and phrase_norm in normalize_title(text))


def topic_relevance(record: dict[str, Any], policy: dict[str, Any] | None) -> tuple[list[str], list[str], list[str]]:
    """Return relevant topics, matched anchors, and exclusion reasons for a record."""
    policy = policy or {}
    rules = policy.get("rules", []) or []
    if not rules:
        fallback_topics = unique_strings((record.get("topics") or []) + [
            item.get("topic", "") for item in (record.get("topic_matches") or []) if isinstance(item, dict)
        ])
        return fallback_topics, (["未配置相关性锚点"] if fallback_topics else []), []

    text = " ".join([clean_text(record.get("title", "")), clean_text(record.get("abstract", ""))])
    global_exclusions = [clean_text(x) for x in policy.get("global_exclude_if_any", []) or [] if clean_text(x)]
    global_hits = [phrase for phrase in global_exclusions if relevance_phrase_in_text(text, phrase)]
    if global_hits:
        return [], [], global_hits

    relevant_topics: list[str] = []
    matched_anchors: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        topic_name = clean_text(rule.get("topic", ""))
        if not topic_name:
            continue
        exclusions = [clean_text(x) for x in rule.get("exclude_if_any", []) or [] if clean_text(x)]
        if any(relevance_phrase_in_text(text, phrase) for phrase in exclusions):
            continue

        groups = rule.get("require_all_groups", []) or []
        if groups:
            group_hits: list[str] = []
            all_groups_matched = True
            for group in groups:
                alternatives = group.get("any_of", []) if isinstance(group, dict) else group
                alternatives = [clean_text(x) for x in (alternatives or []) if clean_text(x)]
                hits = [phrase for phrase in alternatives if relevance_phrase_in_text(text, phrase)]
                if not hits:
                    all_groups_matched = False
                    break
                group_hits.extend(hits)
            if all_groups_matched:
                relevant_topics.append(topic_name)
                matched_anchors.extend(group_hits)
            continue

        anchors = [clean_text(x) for x in rule.get("include_if_any", []) or [] if clean_text(x)]
        hits = [phrase for phrase in anchors if relevance_phrase_in_text(text, phrase)]
        if hits:
            relevant_topics.append(topic_name)
            matched_anchors.extend(hits)
    return unique_strings(relevant_topics), unique_strings(matched_anchors), []


def apply_topic_relevance(record: dict[str, Any], policy: dict[str, Any] | None) -> bool:
    """Preserve retrieval provenance, then replace topic labels with anchor-checked labels."""
    original_topics = record.get("search_topics") or record.get("topics") or []
    if not record.get("search_topics"):
        original_topics = list(original_topics)
        original_topics.extend(
            item.get("topic", "") for item in (record.get("topic_matches") or []) if isinstance(item, dict)
        )
        record["search_topics"] = unique_strings(original_topics)
    relevant_topics, anchors, exclusions = topic_relevance(record, policy)
    record["topics"] = relevant_topics
    record["relevance_anchors"] = anchors
    record["relevance_status"] = "in_scope" if relevant_topics else "out_of_scope"
    record["relevance_exclusion_reasons"] = exclusions or ([] if relevant_topics else ["标题/摘要未命中当前主题锚点"])
    return bool(relevant_topics)


def make_affiliation(author: str, institution: str, source: str, country_code: str = "") -> dict[str, str]:
    institution = clean_text(institution)
    code = (country_code or country_from_text(institution)).upper()
    return {
        "author": clean_text(author),
        "institution": institution,
        "country_code": code,
        "source": source,
    }


def make_institution(name: str, source: str, country_code: str = "") -> dict[str, str]:
    name = clean_text(name)
    code = (country_code or country_from_text(name)).upper()
    return {"name": name, "country_code": code, "source": source}


def http_get(source: str, url: str, params: dict[str, Any], timeout: int, headers: dict[str, str]) -> requests.Response | None:
    for attempt in range(4):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            logging.warning("%s request failed (%s)", source, type(exc).__name__)
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        if response.status_code == 429 or response.status_code >= 500:
            logging.warning("%s temporarily returned HTTP %s", source, response.status_code)
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return None
        if not response.ok:
            logging.warning("%s returned HTTP %s", source, response.status_code)
            return None
        return response
    return None


def base_from_topic(topic: dict[str, Any], query_term: str) -> dict[str, Any]:
    record = new_record()
    record["topics"] = [str(topic.get("name") or "未分类")]
    record["topic_matches"] = [topic_match(topic, query_term)]
    return record


def fetch_arxiv(topic: dict[str, Any], limit: int, cutoff: date, timeout: int, headers: dict[str, str]) -> list[dict[str, Any]]:
    terms = [clean_text(x) for x in topic.get("terms", []) if clean_text(x)]
    categories = [str(x).strip() for x in topic.get("arxiv_categories", []) if str(x).strip()]
    clauses: list[str] = []
    if terms:
        escaped = [term.replace('"', '\\"') for term in terms]
        clauses.append("(" + " OR ".join(f'all:"{term}"' for term in escaped) + ")")
    valid_categories = [cat for cat in categories if re.fullmatch(r"[A-Za-z0-9.-]+", cat)]
    if valid_categories:
        clauses.append("(" + " OR ".join(f"cat:{cat}" for cat in valid_categories) + ")")
    if not clauses:
        return []
    search_query = " AND ".join(clauses)
    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": min(2000, max(1, limit * 3)),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    response = http_get("arXiv", ARXIV_API, params, timeout, headers)
    # arXiv asks API clients to leave at least three seconds between requests.
    time.sleep(3)
    if response is None:
        return []
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        logging.warning("arXiv returned invalid XML")
        return []

    found: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", NS):
        title = clean_text(entry.findtext("atom:title", default="", namespaces=NS))
        if not title:
            continue
        published = parse_date(entry.findtext("atom:published", default="", namespaces=NS))
        updated = parse_date(entry.findtext("atom:updated", default="", namespaces=NS))
        if published and date.fromisoformat(published) < cutoff:
            continue
        entry_url = clean_text(entry.findtext("atom:id", default="", namespaces=NS))
        arxiv_id = extract_arxiv_id(entry_url)
        if not arxiv_id:
            continue

        rec = base_from_topic(topic, "OR: " + " | ".join(terms or valid_categories))
        rec["title"] = title
        rec["abstract"] = clean_text(entry.findtext("atom:summary", default="", namespaces=NS))
        rec["arxiv_id"] = arxiv_id
        rec["doi"] = normalize_doi(entry.findtext("arxiv:doi", default="", namespaces=NS))
        rec["preprint_date"] = published
        rec["preprint_updated_date"] = updated
        rec["last_status_checked"] = today_local().isoformat()
        rec["work_type"] = "preprint"
        rec["source_names"] = ["arXiv"]
        rec["paper_url"] = f"https://arxiv.org/abs/{arxiv_id}"
        rec["open_access_url"] = f"https://arxiv.org/pdf/{arxiv_id}"
        rec["is_open_access"] = True
        rec["source_urls"] = [rec["paper_url"]]
        rec["status_signals"] = [{
            "source": "arXiv",
            "work_type": "preprint",
            "venue": "arXiv",
            "journal_ref": clean_text(entry.findtext("arxiv:journal_ref", default="", namespaces=NS)),
            "doi": normalize_doi(entry.findtext("arxiv:doi", default="", namespaces=NS)),
        }]

        for author_el in entry.findall("atom:author", NS):
            author = clean_text(author_el.findtext("atom:name", default="", namespaces=NS))
            if author:
                rec["authors"].append(author)
            affiliation_el = author_el.find("arxiv:affiliation", NS)
            affiliation = clean_text(affiliation_el.text if affiliation_el is not None else "")
            if affiliation:
                rec["affiliations"].append(make_affiliation(author, affiliation, "arXiv"))
                rec["institutions"].append(make_institution(affiliation, "arXiv"))

        categories_seen = [el.attrib.get("term", "") for el in entry.findall("atom:category", NS)]
        rec["categories"] = [x for x in categories_seen if x]
        found.append(rec)
        if len(found) >= limit:
            break
    return found


def fetch_arxiv_ids(arxiv_ids: list[str], timeout: int, headers: dict[str, str]) -> tuple[bool, list[dict[str, Any]]]:
    """Refresh metadata for known arXiv records so journal_ref/DOI changes are noticed."""
    clean_ids = unique_strings([extract_arxiv_id(value) for value in arxiv_ids])
    clean_ids = [value for value in clean_ids if value]
    if not clean_ids:
        return True, []
    response = http_get(
        "arXiv status refresh",
        ARXIV_API,
        {"id_list": ",".join(clean_ids), "max_results": len(clean_ids)},
        timeout,
        headers,
    )
    time.sleep(3)
    if response is None:
        return False, []
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        logging.warning("arXiv status refresh returned invalid XML")
        return False, []

    refreshed: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", NS):
        title = clean_text(entry.findtext("atom:title", default="", namespaces=NS))
        arxiv_id = extract_arxiv_id(entry.findtext("atom:id", default="", namespaces=NS))
        if not title or not arxiv_id:
            continue
        published = parse_date(entry.findtext("atom:published", default="", namespaces=NS))
        updated = parse_date(entry.findtext("atom:updated", default="", namespaces=NS))
        doi = normalize_doi(entry.findtext("arxiv:doi", default="", namespaces=NS))
        journal_ref = clean_text(entry.findtext("arxiv:journal_ref", default="", namespaces=NS))
        rec = new_record()
        rec.update({
            "title": title,
            "abstract": clean_text(entry.findtext("atom:summary", default="", namespaces=NS)),
            "arxiv_id": arxiv_id,
            "doi": doi,
            "preprint_date": published,
            "preprint_updated_date": updated,
            "last_status_checked": today_local().isoformat(),
            "work_type": "preprint",
            "source_names": ["arXiv"],
            "paper_url": f"https://arxiv.org/abs/{arxiv_id}",
            "open_access_url": f"https://arxiv.org/pdf/{arxiv_id}",
            "is_open_access": True,
            "source_urls": [f"https://arxiv.org/abs/{arxiv_id}"],
            "categories": [el.attrib.get("term", "") for el in entry.findall("atom:category", NS) if el.attrib.get("term")],
            "status_signals": [{
                "source": "arXiv",
                "work_type": "preprint",
                "venue": "arXiv",
                "journal_ref": journal_ref,
                "doi": doi,
            }],
        })
        for author_el in entry.findall("atom:author", NS):
            author = clean_text(author_el.findtext("atom:name", default="", namespaces=NS))
            if author:
                rec["authors"].append(author)
            affiliation_el = author_el.find("arxiv:affiliation", NS)
            affiliation = clean_text(affiliation_el.text if affiliation_el is not None else "")
            if affiliation:
                rec["affiliations"].append(make_affiliation(author, affiliation, "arXiv"))
                rec["institutions"].append(make_institution(affiliation, "arXiv"))
        refreshed.append(rec)
    return True, refreshed


def crossref_date(item: dict[str, Any]) -> str:
    for field in ("published-online", "published-print", "published", "issued"):
        value = parse_date(item.get(field))
        if value:
            return value
    return ""


def fetch_crossref(topic: dict[str, Any], term: str, limit: int, cutoff: date, timeout: int, mailto: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "query.bibliographic": term,
        "rows": min(100, max(1, limit)),
        "filter": f"from-pub-date:{cutoff.isoformat()}",
    }
    if mailto:
        params["mailto"] = mailto
    response = http_get("Crossref", CROSSREF_API, params, timeout, headers)
    time.sleep(0.15)
    if response is None:
        return []
    try:
        items = response.json().get("message", {}).get("items", [])
    except (ValueError, AttributeError):
        logging.warning("Crossref returned invalid JSON")
        return []

    found: list[dict[str, Any]] = []
    for item in items:
        title = clean_text(item.get("title", [""]))
        if not title:
            continue
        pub_date = crossref_date(item)
        if pub_date and date.fromisoformat(pub_date) < cutoff:
            continue
        doi = normalize_doi(item.get("DOI"))
        venue = clean_text(item.get("container-title", [""]))
        work_type = clean_text(item.get("type", ""))
        rec = base_from_topic(topic, term)
        rec["title"] = title
        rec["abstract"] = clean_text(item.get("abstract", ""))
        rec["doi"] = doi
        rec["publication_date"] = pub_date
        rec["venue"] = venue
        rec["work_type"] = work_type
        rec["source_names"] = ["Crossref"]
        rec["paper_url"] = f"https://doi.org/{doi}" if doi else clean_text(item.get("URL", ""))
        rec["source_urls"] = [rec["paper_url"]] if rec["paper_url"] else []
        rec["status_signals"] = [{
            "source": "Crossref",
            "work_type": work_type,
            "venue": venue,
            "venue_type": "",
            "date": pub_date,
            "doi": doi,
        }]

        authors: list[str] = []
        for author_obj in item.get("author", []) or []:
            given = clean_text(author_obj.get("given", ""))
            family = clean_text(author_obj.get("family", ""))
            name = clean_text(" ".join(x for x in (given, family) if x) or author_obj.get("name", ""))
            if name:
                authors.append(name)
            for affiliation_obj in author_obj.get("affiliation", []) or []:
                aff_name = clean_text(affiliation_obj.get("name", ""))
                if aff_name:
                    rec["affiliations"].append(make_affiliation(name, aff_name, "Crossref"))
                    rec["institutions"].append(make_institution(aff_name, "Crossref"))
        rec["authors"] = authors
        found.append(rec)
    return found


def fetch_openalex(topic: dict[str, Any], term: str, limit: int, cutoff: date, timeout: int, api_key: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    if not api_key:
        return []
    params = {
        "search": term,
        "filter": f"from_publication_date:{cutoff.isoformat()}",
        "sort": "publication_date:desc",
        "per-page": min(100, max(1, limit)),
        "api_key": api_key,
    }
    response = http_get("OpenAlex", OPENALEX_API, params, timeout, headers)
    time.sleep(0.15)
    if response is None:
        return []
    try:
        items = response.json().get("results", [])
    except (ValueError, AttributeError):
        logging.warning("OpenAlex returned invalid JSON")
        return []

    found: list[dict[str, Any]] = []
    for item in items:
        title = clean_text(item.get("display_name") or item.get("title"))
        if not title:
            continue
        pub_date = parse_date(item.get("publication_date"))
        if pub_date and date.fromisoformat(pub_date) < cutoff:
            continue
        doi = normalize_doi(item.get("doi") or (item.get("ids") or {}).get("doi"))
        primary = item.get("primary_location") or {}
        source = primary.get("source") or {}
        venue = clean_text(source.get("display_name", ""))
        venue_type = clean_text(source.get("type", ""))
        work_type = clean_text(item.get("type", ""))
        rec = base_from_topic(topic, term)
        rec["title"] = title
        rec["abstract"] = reconstruct_abstract(item.get("abstract_inverted_index"))
        rec["doi"] = doi
        rec["publication_date"] = pub_date
        rec["venue"] = venue
        rec["work_type"] = work_type
        rec["citation_count"] = item.get("cited_by_count")
        rec["source_names"] = ["OpenAlex"]
        rec["paper_url"] = (
            f"https://doi.org/{doi}" if doi else
            clean_text(primary.get("landing_page_url")) or clean_text(item.get("id", ""))
        )
        if rec["paper_url"]:
            rec["source_urls"] = [rec["paper_url"]]
        open_access = item.get("open_access") or {}
        rec["is_open_access"] = open_access.get("is_oa")
        rec["open_access_url"] = clean_text(open_access.get("oa_url", ""))
        rec["status_signals"] = [{
            "source": "OpenAlex",
            "work_type": work_type,
            "venue": venue,
            "venue_type": venue_type,
            "date": pub_date,
            "doi": doi,
        }]

        authors: list[str] = []
        for authorship in item.get("authorships", []) or []:
            author_obj = authorship.get("author") or {}
            author = clean_text(author_obj.get("display_name", ""))
            if author:
                authors.append(author)
            for institution in authorship.get("institutions", []) or []:
                inst_name = clean_text(institution.get("display_name", ""))
                country_code = clean_text(institution.get("country_code", "")).upper()
                if inst_name:
                    rec["institutions"].append(make_institution(inst_name, "OpenAlex", country_code))
                    rec["affiliations"].append(make_affiliation(author, inst_name, "OpenAlex", country_code))
        rec["authors"] = authors
        found.append(rec)
    return found


def unique_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = clean_text(value)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def normalize_institution(value: Any) -> str:
    return normalize_title(value)


def merge_institutions(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in (left or []) + (right or []):
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name") or item.get("institution"))
        if not name:
            continue
        key = normalize_institution(name)
        code = clean_text(item.get("country_code", "")).upper()
        if key not in merged:
            merged[key] = {"name": name, "country_code": code, "sources": []}
        elif not merged[key].get("country_code") and code:
            merged[key]["country_code"] = code
        sources = item.get("sources") or ([item.get("source")] if item.get("source") else [])
        merged[key]["sources"] = unique_strings(merged[key]["sources"] + sources)
    return list(merged.values())


def merge_affiliations(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, str]]:
    merged: dict[tuple[str, str], dict[str, str]] = {}
    for item in (left or []) + (right or []):
        if not isinstance(item, dict):
            continue
        author = clean_text(item.get("author", ""))
        inst = clean_text(item.get("institution", ""))
        if not inst:
            continue
        key = (normalize_title(author), normalize_title(inst))
        code = clean_text(item.get("country_code", "")).upper() or country_from_text(inst)
        if key not in merged:
            merged[key] = {
                "author": author,
                "institution": inst,
                "country_code": code,
                "source": clean_text(item.get("source", "")),
            }
        elif not merged[key].get("country_code") and code:
            merged[key]["country_code"] = code
        elif not merged[key].get("source") and item.get("source"):
            merged[key]["source"] = clean_text(item.get("source"))
    return list(merged.values())


def merge_dict_signals(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in (left or []) + (right or []):
        if not isinstance(item, dict):
            continue
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def merge_topic_matches(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in (left or []) + (right or []):
        if not isinstance(item, dict):
            continue
        normalized = {
            "topic": clean_text(item.get("topic", "")),
            "role": clean_text(item.get("role", "adjacent")),
            "weight": item.get("weight", 1.0),
            "query_term": clean_text(item.get("query_term", "")),
        }
        key = (normalized["topic"], normalized["role"], normalized["query_term"])
        if normalized["topic"] and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def merge_record(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge source records while preserving user-authored reading status/notes."""
    result = dict(existing)
    result["title"] = result.get("title") or incoming.get("title", "")
    if len(clean_text(incoming.get("abstract"))) > len(clean_text(result.get("abstract"))):
        result["abstract"] = clean_text(incoming.get("abstract"))

    result["authors"] = unique_strings((result.get("authors") or []) + (incoming.get("authors") or []))
    result["institutions"] = merge_institutions(result.get("institutions", []), incoming.get("institutions", []))
    result["affiliations"] = merge_affiliations(result.get("affiliations", []), incoming.get("affiliations", []))
    result["institution_countries"] = unique_strings(
        (result.get("institution_countries") or [])
        + [x.get("country_code", "") for x in result["institutions"] if x.get("country_code")]
        + [x.get("country_code", "") for x in result["affiliations"] if x.get("country_code")]
    )

    if not result.get("arxiv_id") and incoming.get("arxiv_id"):
        result["arxiv_id"] = incoming["arxiv_id"]
    incoming_doi = normalize_doi(incoming.get("doi"))
    existing_doi = normalize_doi(result.get("doi"))
    if incoming_doi and (not existing_doi or (is_arxiv_doi(existing_doi) and not is_arxiv_doi(incoming_doi))):
        result["doi"] = incoming_doi
    incoming_type = clean_text(incoming.get("work_type", ""))
    if incoming_type and (not result.get("work_type") or (result.get("work_type") == "preprint" and incoming_type != "preprint")):
        result["work_type"] = incoming_type
    result["categories"] = unique_strings((result.get("categories") or []) + (incoming.get("categories") or []))
    for field in ("publication_date", "preprint_date"):
        values = [x for x in (result.get(field), incoming.get(field)) if x]
        if values:
            result[field] = min(values)
    updates = [x for x in (result.get("preprint_updated_date"), incoming.get("preprint_updated_date")) if x]
    if updates:
        result["preprint_updated_date"] = max(updates)

    incoming_venue = clean_text(incoming.get("venue", ""))
    if incoming_venue:
        if not result.get("venue") or PREPRINT_RE.search(result.get("venue", "")):
            result["venue"] = incoming_venue
        elif not PREPRINT_RE.search(incoming_venue) and (incoming.get("source_names") or [""])[0] in ("Crossref", "OpenAlex"):
            result["venue"] = incoming_venue

    result["source_names"] = unique_strings((result.get("source_names") or []) + (incoming.get("source_names") or []))
    result["source_urls"] = unique_strings((result.get("source_urls") or []) + (incoming.get("source_urls") or []))
    result["topics"] = unique_strings((result.get("topics") or []) + (incoming.get("topics") or []))
    result["search_topics"] = unique_strings((result.get("search_topics") or []) + (incoming.get("search_topics") or []))
    result["relevance_anchors"] = unique_strings((result.get("relevance_anchors") or []) + (incoming.get("relevance_anchors") or []))
    result["topic_matches"] = merge_topic_matches(result.get("topic_matches", []), incoming.get("topic_matches", []))
    result["status_signals"] = merge_dict_signals(result.get("status_signals", []), incoming.get("status_signals", []))
    if incoming.get("paper_url") and (not result.get("paper_url") or (incoming.get("doi") and not is_arxiv_doi(incoming.get("doi", "")))):
        result["paper_url"] = incoming["paper_url"]
    if incoming.get("open_access_url") and not result.get("open_access_url"):
        result["open_access_url"] = incoming["open_access_url"]
    if incoming.get("is_open_access") is True:
        result["is_open_access"] = True
    elif result.get("is_open_access") is None and incoming.get("is_open_access") is not None:
        result["is_open_access"] = incoming.get("is_open_access")
    if incoming.get("citation_count") is not None:
        try:
            old_count = int(result.get("citation_count") or 0)
            result["citation_count"] = max(old_count, int(incoming["citation_count"]))
        except (TypeError, ValueError):
            result["citation_count"] = incoming.get("citation_count")

    result["first_seen"] = result.get("first_seen") or incoming.get("first_seen") or today_local().isoformat()
    checked_dates = [x for x in (result.get("last_status_checked"), incoming.get("last_status_checked")) if x]
    if checked_dates:
        result["last_status_checked"] = max(checked_dates)
    # These are user-maintained fields; incoming API records never overwrite them.
    result["reading_status"] = existing.get("reading_status", "未读")
    result["my_notes"] = existing.get("my_notes", "")
    if not result.get("uid"):
        result["uid"] = incoming.get("uid", "")
    return result


def identifier_keys(record: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    doi = normalize_doi(record.get("doi"))
    if doi and not is_arxiv_doi(doi):
        keys.append("doi:" + doi)
    arxiv_id = extract_arxiv_id(record.get("arxiv_id"))
    if arxiv_id:
        keys.append("arxiv:" + arxiv_id)
    title = normalize_title(record.get("title"))
    if len(title) >= 12:
        keys.append("title:" + title)
    return keys


def stable_uid(record: dict[str, Any]) -> str:
    arxiv_id = extract_arxiv_id(record.get("arxiv_id"))
    if arxiv_id:
        return "arxiv:" + arxiv_id
    doi = normalize_doi(record.get("doi"))
    if doi:
        return "doi:" + doi
    title = normalize_title(record.get("title"))
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:14]
    return "title:" + digest


def build_index(papers: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, list[int]]]:
    ids: dict[str, int] = {}
    titles: dict[str, list[int]] = {}
    for index, paper in enumerate(papers):
        for key in identifier_keys(paper):
            if key.startswith("title:"):
                titles.setdefault(key, []).append(index)
            else:
                ids.setdefault(key, index)
    return ids, titles


def record_year(record: dict[str, Any]) -> int | None:
    value = record.get("publication_date") or record.get("preprint_date") or ""
    try:
        return int(str(value)[:4]) if value else None
    except ValueError:
        return None


def find_existing(record: dict[str, Any], papers: list[dict[str, Any]], ids: dict[str, int], titles: dict[str, list[int]]) -> int | None:
    for key in identifier_keys(record):
        if not key.startswith("title:") and key in ids:
            return ids[key]
    title_key = next((key for key in identifier_keys(record) if key.startswith("title:")), "")
    if title_key:
        incoming_year = record_year(record)
        candidates = titles.get(title_key, [])
        for index in candidates:
            existing_year = record_year(papers[index])
            if incoming_year is None or existing_year is None or abs(incoming_year - existing_year) <= 2:
                return index
    return None


def classify_status(record: dict[str, Any]) -> tuple[str, list[str]]:
    signals = record.get("status_signals", []) or []
    formal: list[str] = []
    accepted: list[str] = []
    has_preprint = bool(record.get("arxiv_id"))

    for signal in signals:
        if not isinstance(signal, dict):
            continue
        source = clean_text(signal.get("source", ""))
        venue = clean_text(signal.get("venue", ""))
        venue_type = clean_text(signal.get("venue_type", "")).casefold()
        work_type = clean_text(signal.get("work_type", "")).casefold()
        doi = normalize_doi(signal.get("doi", ""))
        journal_ref = clean_text(signal.get("journal_ref", ""))
        if PREPRINT_RE.search(venue) or "preprint" in work_type or work_type == "posted-content":
            has_preprint = True

        evidence_text = ""
        if journal_ref and ACCEPTED_RE.search(journal_ref):
            accepted.append(f"arXiv 元数据明确写明：{journal_ref}")
        elif journal_ref and not NOT_PUBLISHED_RE.search(journal_ref):
            # arXiv's journal_ref is author-provided. Keep the evidence label explicit.
            formal.append(f"arXiv journal_ref（作者提供，需核验）：{journal_ref}")

        if source == "Crossref" and doi and not is_arxiv_doi(doi) and venue and not PREPRINT_RE.search(venue):
            if work_type not in ("posted-content", "preprint"):
                evidence_text = f"Crossref：{work_type or '出版条目'}；{venue}"
                if signal.get("date"):
                    evidence_text += f"；日期 {signal['date']}"
                formal.append(evidence_text)
        elif source == "OpenAlex" and venue and not PREPRINT_RE.search(venue):
            if venue_type in ("journal", "conference") and work_type != "preprint":
                evidence_text = f"OpenAlex：{venue_type} 来源 {venue}"
                if signal.get("date"):
                    evidence_text += f"；日期 {signal['date']}"
                formal.append(evidence_text)

    formal = unique_strings(formal)
    accepted = unique_strings(accepted)
    if formal:
        return "已发表/有期刊或会议记录（建议核验）", formal
    if accepted:
        return "已接收/待刊（元数据明确注明）", accepted
    if has_preprint:
        return "arXiv/预印本（暂未检出正式出版记录）", ["发现预印本记录；未从当前来源检出正式出版信息，这不等于确定未发表。"]
    return "未知（元数据不足）", ["当前来源没有提供足以判断发表状态的信息。"]


def compute_home_scope(record: dict[str, Any], domestic_codes: set[str]) -> tuple[list[str], str]:
    countries = set()
    for inst in record.get("institutions", []) or []:
        if isinstance(inst, dict) and inst.get("country_code"):
            countries.add(str(inst["country_code"]).upper())
    for affiliation in record.get("affiliations", []) or []:
        if isinstance(affiliation, dict) and affiliation.get("country_code"):
            countries.add(str(affiliation["country_code"]).upper())
    countries.update(str(code).upper() for code in (record.get("institution_countries") or []) if code)
    ordered = sorted(countries)
    home = countries & domestic_codes
    away = countries - domestic_codes
    if not countries:
        scope = "未知"
    elif home and away:
        scope = "国内+国外合作"
    elif home:
        scope = "国内"
    else:
        scope = "国外"
    return ordered, scope


def finalize_record(record: dict[str, Any], domestic_codes: set[str]) -> dict[str, Any]:
    record["doi"] = normalize_doi(record.get("doi"))
    record["arxiv_id"] = extract_arxiv_id(record.get("arxiv_id"))
    record["authors"] = unique_strings(record.get("authors", []))
    record["institutions"] = merge_institutions([], record.get("institutions", []))
    record["affiliations"] = merge_affiliations([], record.get("affiliations", []))
    countries, scope = compute_home_scope(record, domestic_codes)
    record["institution_countries"] = countries
    record["home_scope"] = scope
    status, evidence = classify_status(record)
    record["publication_status"] = status
    record["status_evidence"] = evidence
    record["peer_review_status"] = "需人工核验；数据库出版记录不等同于审稿结论"
    record["topics"] = unique_strings(record.get("topics", []))
    record["source_names"] = unique_strings(record.get("source_names", []))
    record["source_urls"] = unique_strings(record.get("source_urls", []))
    if not record.get("paper_url"):
        record["paper_url"] = record["source_urls"][0] if record["source_urls"] else ""
    if not record.get("uid"):
        record["uid"] = stable_uid(record)
    return record


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"找不到配置文件：{path}")
    with path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    if not isinstance(config, dict):
        raise ValueError("配置文件顶层必须是 YAML 字典")
    return config


def load_existing(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("无法读取已有文献库（%s）；将创建新库。", type(exc).__name__)
        return []


def markdown_paper_card(paper: dict[str, Any], number: int, abstract_limit: int = 500) -> list[str]:
    title = clean_text(paper.get("title", "")) or "无标题"
    link = clean_text(paper.get("paper_url", ""))
    shown_title = f"[{title}]({link})" if link else title
    authors = "、".join((paper.get("authors") or [])[:8])
    if len(paper.get("authors") or []) > 8:
        authors += " 等"
    institutions = []
    for inst in paper.get("institutions", []) or []:
        if isinstance(inst, dict) and inst.get("name"):
            code = clean_text(inst.get("country_code", ""))
            label = clean_text(inst.get("name", ""))
            institutions.append(f"{label} [{country_label(code)}]" if code else label)
    abstract = clean_text(paper.get("abstract", ""))
    if len(abstract) > abstract_limit:
        abstract = abstract[:abstract_limit].rstrip() + "…"
    evidence = "；".join(paper.get("status_evidence") or [])
    if len(evidence) > 260:
        evidence = evidence[:260].rstrip() + "…"
    lines = [
        f"### {number}. {shown_title}",
        f"- **主题：**{'、'.join(paper.get('topics') or []) or '未分类'}　 **机构范围：**{paper.get('home_scope', '未知')}",
        f"- **状态：**{paper.get('publication_status', '未知')}",
        f"- **日期：**出版 {paper.get('publication_date') or '未知'}；预印本 {paper.get('preprint_date') or '—'}；预印本更新 {paper.get('preprint_updated_date') or '—'}",
        f"- **作者：**{authors or '未提供'}",
        f"- **机构：**{'；'.join(unique_strings(institutions)) or '未提供/未识别'}",
        f"- **期刊/会议：**{paper.get('venue') or '—'}　 **DOI：**{paper.get('doi') or '—'}　 **arXiv：**{paper.get('arxiv_id') or '—'}",
        f"- **来源：**{'、'.join(paper.get('source_names') or []) or '未知'}　 **引用数：**{paper.get('citation_count') if paper.get('citation_count') is not None else '—'}",
    ]
    if evidence:
        lines.append(f"- **状态依据：**{evidence}")
    if paper.get("open_access_url"):
        lines.append(f"- **开放获取：**[查看链接]({paper['open_access_url']})")
    if abstract:
        lines.append(f"- **摘要：**{abstract}")
    return lines + [""]


def render_digest(papers: list[dict[str, Any]], title: str, empty_message: str, cutoff: date, note: str) -> str:
    lines = [title, "", f"检索窗口：{cutoff.isoformat()} 起。生成日期：{today_local().isoformat()}（Asia/Shanghai）。", f"> {note}", ""]
    if not papers:
        lines.append(empty_message)
    else:
        for number, paper in enumerate(papers, 1):
            lines.extend(markdown_paper_card(paper, number))
    return "\n".join(lines).rstrip() + "\n"


def render_library(papers: list[dict[str, Any]]) -> str:
    today = today_local().isoformat()
    lines = [
        f"# 文献库｜累计 {len(papers)} 篇",
        "",
        f"最后生成：{today}（Asia/Shanghai）。按出版/预印本日期倒序排列。",
        "> 本文件是便于阅读的完整目录；`papers.json` 保存结构化数据，`daily/` 保存按日新增记录。",
        "",
    ]
    if not papers:
        lines.append("目前还没有收录文献。")
        return "\n".join(lines) + "\n"
    by_year: dict[str, list[dict[str, Any]]] = {}
    for paper in papers:
        date_value = paper.get("publication_date") or paper.get("preprint_date") or paper.get("first_seen") or ""
        year = str(date_value)[:4] if str(date_value)[:4].isdigit() else "年份未知"
        by_year.setdefault(year, []).append(paper)
    for year in sorted(by_year, reverse=True):
        lines.extend([f"## {year}", ""])
        for number, paper in enumerate(by_year[year], 1):
            lines.extend(markdown_paper_card(paper, number))
    return "\n".join(lines).rstrip() + "\n"


def write_outputs(papers: list[dict[str, Any]], new_papers: list[dict[str, Any]], output_dir: Path, cutoff: date) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "papers.json"
    csv_path = output_dir / "papers.csv"
    digest_path = output_dir / "new_this_run.md"
    library_path = output_dir / "papers.md"
    daily_dir = output_dir / "daily"

    ordered = sorted(
        papers,
        key=lambda p: (p.get("publication_date") or p.get("preprint_date") or p.get("first_seen") or "", p.get("title", "")),
        reverse=True,
    )
    json_path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    library_path.write_text(render_library(ordered), encoding="utf-8")

    # Keep one durable Markdown file per date with all records first discovered
    # on that date. Re-running on the same date reconstructs the complete daily file.
    today_str = today_local().isoformat()
    today_papers = [p for p in ordered if str(p.get("first_seen", ""))[:10] == today_str]
    if today_papers:
        daily_dir.mkdir(parents=True, exist_ok=True)
        daily_path = daily_dir / f"{today_str}.md"
        daily_path.write_text(
            render_digest(
                today_papers,
                f"# 文献雷达｜{today_str} 新增 {len(today_papers)} 篇",
                "当天没有新增文献。",
                cutoff,
                "按首次收录日期保存；同一天重新运行会重建当天的完整摘要。",
            ),
            encoding="utf-8",
        )

    columns = [
        ("uid", "记录ID"), ("title", "标题"), ("authors", "作者"),
        ("institutions", "机构"), ("institution_countries", "机构国家/地区"),
        ("home_scope", "国内/国外"), ("publication_status", "发表状态"),
        ("status_evidence", "状态依据"), ("publication_date", "出版日期"),
        ("preprint_date", "预印本首次提交日期"), ("preprint_updated_date", "预印本更新日期"),
        ("last_status_checked", "arXiv状态上次检查"), ("venue", "期刊/会议/来源"), ("doi", "DOI"), ("arxiv_id", "arXiv ID"),
        ("topics", "复核后主题"), ("search_topics", "原始检索命中主题"),
        ("relevance_status", "主题范围判断"), ("relevance_anchors", "相关性锚点"),
        ("relevance_exclusion_reasons", "范围外原因"),
        ("categories", "arXiv 分类"), ("source_names", "元数据来源"), ("paper_url", "论文链接"),
        ("open_access_url", "开放获取链接"), ("is_open_access", "开放获取标记"),
        ("citation_count", "引用数（OpenAlex）"), ("reading_status", "阅读状态（可手动维护）"),
        ("my_notes", "我的笔记（可手动维护）"), ("abstract", "摘要"),
    ]
    # Keep the spreadsheet focused on in-scope papers; the complete audit trail,
    # including legacy out-of-scope records, remains in papers.json.
    csv_papers = [paper for paper in ordered if paper.get("relevance_status") != "out_of_scope"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([label for _, label in columns])
        for paper in csv_papers:
            row: list[str] = []
            for key, _ in columns:
                value = paper.get(key, "")
                if key == "authors" or key in ("topics", "search_topics", "relevance_anchors", "relevance_exclusion_reasons", "categories", "source_names", "institution_countries", "status_evidence"):
                    value = "；".join(str(x) for x in (value or []))
                elif key == "institutions":
                    pieces = []
                    for inst in value or []:
                        if isinstance(inst, dict):
                            label = clean_text(inst.get("name", ""))
                            code = clean_text(inst.get("country_code", ""))
                            if label:
                                pieces.append(f"{label} [{country_label(code)}]" if code else label)
                    value = "；".join(unique_strings(pieces))
                elif value is None:
                    value = ""
                elif isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                row.append(str(value))
            writer.writerow(row)

    if not new_papers:
        digest = (
            "# 文献雷达：本次检索未发现新增文献\n\n"
            f"检索窗口：{cutoff.isoformat()} 起。累计目录见 `papers.md`；结构化数据见 `papers.json`。\n"
        )
    else:
        digest_lines = [
            f"# 文献雷达｜本次新增 {len(new_papers)} 篇",
            "",
            f"检索窗口：{cutoff.isoformat()} 起。生成日期：{today_local().isoformat()}（Asia/Shanghai）。",
            "> 本文件每次运行都会覆盖，仅包含本轮新增；按天保留的历史摘要在 `daily/`，累计目录见 `papers.md`。",
            "",
        ]
        for number, paper in enumerate(new_papers, 1):
            title = clean_text(paper.get("title", "")) or "无标题"
            link = clean_text(paper.get("paper_url", ""))
            shown_title = f"[{title}]({link})" if link else title
            authors = "、".join((paper.get("authors") or [])[:8])
            if len(paper.get("authors") or []) > 8:
                authors += " 等"
            inst_names = []
            for inst in paper.get("institutions", []) or []:
                if isinstance(inst, dict) and inst.get("name"):
                    code = inst.get("country_code", "")
                    inst_names.append(f"{inst['name']} [{code}]" if code else inst["name"])
            abstract = clean_text(paper.get("abstract", ""))
            if len(abstract) > 500:
                abstract = abstract[:500].rstrip() + "…"
            digest_lines.extend([
                f"## {number}. {shown_title}",
                f"- 主题：{'、'.join(paper.get('topics') or []) or '未分类'}；机构范围：{paper.get('home_scope', '未知')}",
                f"- 状态：{paper.get('publication_status', '未知')}",
                f"- 日期：出版 {paper.get('publication_date') or '未知'}；预印本 {paper.get('preprint_date') or '—'}",
                f"- 作者：{authors or '未提供'}",
                f"- 机构：{'；'.join(unique_strings(inst_names)) or '未提供/未识别'}",
                f"- DOI：{paper.get('doi') or '—'}；arXiv：{paper.get('arxiv_id') or '—'}",
                f"- 来源：{'、'.join(paper.get('source_names') or []) or '未知'}",
                f"- 摘要：{abstract or '来源未提供摘要'}",
                "",
            ])
        digest = "\n".join(digest_lines).rstrip() + "\n"
    digest_path.write_text(digest, encoding="utf-8")
    logging.info("已写入 %s 篇文献：%s", len(ordered), output_dir)


def send_feishu(webhook_url: str, new_papers: list[dict[str, Any]], limit: int, timeout: int) -> None:
    if not webhook_url or not new_papers:
        return
    lines = [f"【文献雷达】新增 {len(new_papers)} 篇（最多展示 {min(limit, len(new_papers))} 篇）", ""]
    for index, paper in enumerate(new_papers[:limit], 1):
        title = clean_text(paper.get("title", "无标题"))
        lines.append(f"{index}. {title}")
        lines.append(f"状态：{paper.get('publication_status', '未知')}；领域：{'、'.join(paper.get('topics') or [])}")
        lines.append(f"日期：{paper.get('publication_date') or paper.get('preprint_date') or '未知'}；机构：{paper.get('home_scope', '未知')}")
        link = clean_text(paper.get("paper_url", ""))
        if link:
            lines.append(link)
        abstract = clean_text(paper.get("abstract", ""))
        if abstract:
            lines.append("摘要：" + (abstract[:220].rstrip() + "…" if len(abstract) > 220 else abstract))
        lines.append("")
    if len(new_papers) > limit:
        lines.append(f"另有 {len(new_papers) - limit} 篇，请查看 GitHub 仓库的 data/papers.md。")
    try:
        response = requests.post(
            webhook_url,
            json={"msg_type": "text", "content": {"text": "\n".join(lines)}},
            headers={"User-Agent": APP_NAME},
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code", 0) not in (0, None):
            logging.warning("飞书机器人返回错误码：%s", body.get("code"))
        else:
            logging.info("飞书通知已发送")
    except (requests.RequestException, ValueError) as exc:
        logging.warning("飞书通知失败（%s）", type(exc).__name__)


def run(config_path: Path) -> int:
    config = load_config(config_path)
    config_dir = config_path.resolve().parent
    output_dir = Path(config.get("output_dir", "data"))
    if not output_dir.is_absolute():
        output_dir = config_dir / output_dir

    lookback_days = max(1, int(config.get("lookback_days", 14)))
    limit = max(1, min(100, int(config.get("max_results_per_query", 25))))
    timeout = max(5, int(config.get("request_timeout_seconds", 30)))
    cutoff = today_local() - timedelta(days=lookback_days)
    sources = config.get("sources", {}) or {}
    topics = config.get("topics", []) or []
    relevance_policy = config.get("relevance_policy", {}) or {}
    domestic_codes = {str(x).upper() for x in config.get("domestic_country_codes", ["CN"])}
    if not domestic_codes:
        domestic_codes = {"CN"}

    mailto = os.getenv("CROSSREF_MAILTO", "").strip() or str(config.get("crossref_mailto", "")).strip()
    openalex_key = os.getenv("OPENALEX_API_KEY", "").strip()
    user_agent_email = mailto or ""
    headers = {"User-Agent": f"{APP_NAME}" + (f" (mailto:{user_agent_email})" if user_agent_email else "")}

    json_path = output_dir / "papers.json"
    papers = load_existing(json_path)
    for paper in papers:
        if not paper.get("uid"):
            paper["uid"] = stable_uid(paper)
        finalize_record(paper, domestic_codes)
        apply_topic_relevance(paper, relevance_policy)
    ids, titles = build_index(papers)
    new_uids: set[str] = set()
    attempted = 0
    relevance_rejected = 0

    for topic in topics:
        if not isinstance(topic, dict):
            continue
        terms = [clean_text(x) for x in topic.get("terms", []) if clean_text(x)]
        if sources.get("arxiv", True):
            for incoming in fetch_arxiv(topic, limit, cutoff, timeout, headers):
                attempted += 1
                if not apply_topic_relevance(incoming, relevance_policy):
                    relevance_rejected += 1
                    continue
                merge_incoming(incoming, papers, ids, titles, new_uids, domestic_codes)
        for term in terms:
            if sources.get("crossref", True):
                for incoming in fetch_crossref(topic, term, limit, cutoff, timeout, mailto, headers):
                    attempted += 1
                    if not apply_topic_relevance(incoming, relevance_policy):
                        relevance_rejected += 1
                        continue
                    merge_incoming(incoming, papers, ids, titles, new_uids, domestic_codes)
            if sources.get("openalex", True):
                if not openalex_key:
                    # One reminder is enough; Crossref and arXiv still run without this key.
                    continue
                for incoming in fetch_openalex(topic, term, limit, cutoff, timeout, openalex_key, headers):
                    attempted += 1
                    if not apply_topic_relevance(incoming, relevance_policy):
                        relevance_rejected += 1
                        continue
                    merge_incoming(incoming, papers, ids, titles, new_uids, domestic_codes)

    # Periodically recheck known arXiv preprints: arXiv records may later gain a
    # journal_ref or DOI. This keeps old papers from remaining "preprint" forever.
    refreshed_count = 0
    if sources.get("arxiv", True):
        status_interval = max(1, int(config.get("status_recheck_interval_days", 30)))
        max_status_checks = max(1, int(config.get("max_arxiv_status_checks_per_run", 100)))
        due_before = today_local() - timedelta(days=status_interval)
        stale = []
        for paper in papers:
            if not paper.get("arxiv_id"):
                continue
            if str(paper.get("publication_status", "")).startswith("已发表/"):
                continue
            last_checked = parse_date(paper.get("last_status_checked"))
            if not last_checked or date.fromisoformat(last_checked) <= due_before:
                stale.append(paper)
        stale.sort(key=lambda p: p.get("last_status_checked") or "0000-00-00")
        stale_ids = unique_strings([p.get("arxiv_id", "") for p in stale])[:max_status_checks]
        batch_size = min(50, max_status_checks)
        for start in range(0, len(stale_ids), batch_size):
            batch = stale_ids[start:start + batch_size]
            success, refreshed = fetch_arxiv_ids(batch, timeout, headers)
            if not success:
                continue
            today_str = today_local().isoformat()
            batch_set = set(batch)
            for paper in papers:
                if paper.get("arxiv_id") in batch_set:
                    paper["last_status_checked"] = today_str
            for incoming in refreshed:
                attempted += 1
                merge_incoming(incoming, papers, ids, titles, new_uids, domestic_codes)
                refreshed_count += 1

    # Recalculate derived status/country/relevance fields after all sources have been merged.
    papers = [finalize_record(paper, domestic_codes) for paper in papers]
    for paper in papers:
        apply_topic_relevance(paper, relevance_policy)
    new_papers = [paper for paper in papers if paper.get("uid") in new_uids]
    new_papers.sort(key=lambda p: (p.get("publication_date") or p.get("preprint_date") or "", p.get("title", "")), reverse=True)
    write_outputs(papers, new_papers, output_dir, cutoff)

    if sources.get("openalex", True) and not openalex_key:
        logging.info("未设置 OPENALEX_API_KEY，跳过 OpenAlex；arXiv 与 Crossref 仍可运行。")
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if config.get("feishu", {}).get("enabled", False) and not webhook:
        logging.warning("飞书通知已开启，但未设置 FEISHU_WEBHOOK_URL 环境变量。")
    if webhook:
        send_feishu(webhook, new_papers, int(config.get("max_papers_in_feishu", 8)), timeout)

    logging.info("检索完成：本轮取回 %s 条候选，按主题锚点筛除 %s 条，刷新 arXiv 状态 %s 条，新增 %s 篇，累计 %s 篇。", attempted, relevance_rejected, refreshed_count, len(new_papers), len(papers))
    return 0


def merge_incoming(
    incoming: dict[str, Any],
    papers: list[dict[str, Any]],
    ids: dict[str, int],
    titles: dict[str, list[int]],
    new_uids: set[str],
    domestic_codes: set[str],
) -> None:
    index = find_existing(incoming, papers, ids, titles)
    if index is None:
        incoming["uid"] = stable_uid(incoming)
        incoming = finalize_record(incoming, domestic_codes)
        papers.append(incoming)
        index = len(papers) - 1
        new_uids.add(incoming["uid"])
    else:
        papers[index] = merge_record(papers[index], incoming)
        papers[index] = finalize_record(papers[index], domestic_codes)
    for key in identifier_keys(papers[index]):
        if key.startswith("title:"):
            indexes = titles.setdefault(key, [])
            if index not in indexes:
                indexes.append(index)
        else:
            ids.setdefault(key, index)


def main() -> int:
    parser = argparse.ArgumentParser(description="自动检索并整理 arXiv / Crossref / OpenAlex 文献元数据")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径，默认 config.yaml")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run(Path(args.config))
    except Exception as exc:
        logging.exception("运行失败：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

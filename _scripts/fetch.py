# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx",
#   "markdownify",
#   "pyyaml",
#   "pycryptodome",
# ]
# ///
"""Fetch AI-lab careers pages into per-org markdown archives.

Layout produced (under --out, default ./content):

    <slug>/
        active/    {id}.md   -- currently-listed roles
        archived/  {id}.md   -- roles that have dropped off the live list

The job's stable source id is the filename, so reruns are idempotent and
git diffs show only real changes. When a role disappears from the live
list it's moved active/ -> archived/; if it reappears it moves back.

Usage:
    uv run _scripts/fetch.py [--out content] [--sources sources.json] [--only anthropic]
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from markdownify import markdownify as md

UA = "hirint/0.1 (+https://github.com/marcus-crane/hirint)"
# Feishu/Lark Hire rejects non-browser UAs; it serves HTML instead of JSON.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = httpx.Timeout(30.0, read=90.0)


@dataclass
class Job:
    id: str
    title: str
    url: str
    source: str
    location: str | None = None
    departments: list[str] = field(default_factory=list)
    offices: list[str] = field(default_factory=list)
    compensation: str | None = None  # display summary, e.g. "$182.7K – $247.5K • Offers Equity"
    date: str | None = None       # first published
    lastmod: str | None = None    # source-side last update
    body_html: str = ""

    def frontmatter(self, status: str) -> dict:
        fm: dict = {"title": self.title, "status": status, "id": self.id, "source": self.source, "url": self.url}
        if self.lastmod:
            fm["lastmod"] = self.lastmod
        if self.date:
            fm["date"] = self.date
        if self.location:
            fm["location"] = self.location
        # Sort the list fields: sources return these in unstable order, which would
        # otherwise manufacture diffs on re-fetch (a reshuffle isn't a real change).
        if self.departments:
            fm["departments"] = sorted(set(self.departments))
        if self.offices:
            fm["offices"] = sorted(set(self.offices))
        if self.compensation:
            fm["compensation"] = self.compensation
        return fm

    def to_markdown(self, status: str = "active") -> str:
        front = yaml.safe_dump(self.frontmatter(status), sort_keys=False, allow_unicode=True).strip()
        body = md(self.body_html, heading_style="ATX").strip()
        return f"---\n{front}\n---\n\n{body}\n"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# --- adapters ---------------------------------------------------------------

def get_json(client: httpx.Client, url: str, *, headers: dict | None = None, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            return client.get(url, headers=headers).raise_for_status().json()
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def post_json(client: httpx.Client, url: str, *, headers: dict, json: dict, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            return client.post(url, headers=headers, json=json).raise_for_status().json()
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def post_form(client: httpx.Client, url: str, *, headers: dict, data: dict, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            return client.post(url, headers=headers, data=data).raise_for_status().json()
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def fetch_greenhouse(client: httpx.Client, board: str) -> list[Job]:
    # Greenhouse returns the `content` field HTML-entity-escaped.
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
    data = get_json(client, url)
    jobs = []
    for j in data["jobs"]:
        jobs.append(
            Job(
                id=str(j["id"]),
                title=j["title"],
                url=j["absolute_url"],
                source="greenhouse",
                location=(j.get("location") or {}).get("name"),
                departments=[d["name"] for d in j.get("departments", []) if d.get("name")],
                offices=[o["name"] for o in j.get("offices", []) if o.get("name")],
                date=j.get("first_published"),
                lastmod=j.get("updated_at"),
                body_html=html.unescape(j.get("content", "")),
            )
        )
    return jobs


def fetch_ashby(client: httpx.Client, board: str) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
    data = get_json(client, url)
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        secondary = [s.get("location") for s in j.get("secondaryLocations", []) if s.get("location")]
        comp = j.get("compensation") or {}
        comp_summary = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary")
        jobs.append(
            Job(
                id=str(j["id"]),
                title=j["title"],
                url=j.get("jobUrl", ""),
                source="ashby",
                location=j.get("location"),
                departments=list(dict.fromkeys(d for d in (j.get("department"), j.get("team")) if d)),
                offices=secondary,
                compensation=comp_summary,
                date=j.get("publishedDate") or j.get("publishedAt"),
                lastmod=j.get("publishedDate") or j.get("publishedAt"),
                body_html=j.get("descriptionHtml") or j.get("description") or "",
            )
        )
    return jobs


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _text_to_html(text: str | None) -> str:
    """Feishu serves description/requirement as plain text with newlines."""
    lines = (text or "").splitlines()
    return "".join(f"<p>{html.escape(line)}</p>" for line in lines if line.strip())


def fetch_feishu(client: httpx.Client, board: str) -> list[Job]:
    # `board` is the portal subdomain host, e.g. "01ai.jobs.feishu.cn".
    # All Lark Hire (saas-career) portals share this API surface.
    origin = board if board.startswith("http") else f"https://{board}"
    headers = {
        "User-Agent": BROWSER_UA,
        "Referer": origin + "/",
        "Origin": origin,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    client.get(origin + "/", headers={"User-Agent": BROWSER_UA})  # seed cookies; bare UA gets HTML

    jobs: list[Job] = []
    offset, limit = 0, 50
    while True:
        body = {"offset": offset, "limit": limit}
        resp = post_json(client, f"{origin}/api/v1/search/job/posts", headers=headers, json=body)
        data = resp.get("data", {})
        posts = data.get("job_post_list") or []
        for j in posts:
            jid = str(j["id"])
            cities = [c.get("name") for c in (j.get("city_list") or []) if c.get("name")]
            category = (j.get("job_category") or {}).get("name")
            body_html = (
                f"<h2>职位描述</h2>{_text_to_html(j.get('description'))}"
                f"<h2>任职要求</h2>{_text_to_html(j.get('requirement'))}"
            )
            published = _ms_to_iso(j.get("publish_time"))
            jobs.append(
                Job(
                    id=jid,
                    title=j["title"],
                    url=f"{origin}/position/{jid}",
                    source="feishu",
                    location=", ".join(cities) or None,
                    departments=[category] if category else [],
                    offices=cities,
                    date=published,
                    lastmod=published,
                    body_html=body_html,
                )
            )
        offset += limit
        if not posts or offset >= data.get("count", 0):
            break
    return jobs


def _moka_locations(raw) -> list[str]:
    out = []
    for loc in raw or []:
        if isinstance(loc, dict):
            name = loc.get("name") or loc.get("cityName") or loc.get("city")
            if name:
                out.append(name)
        elif isinstance(loc, str):
            out.append(loc)
    return out


def fetch_moka(client: httpx.Client, board: str) -> list[Job]:
    # `board` is "{org}/{siteId}", e.g. "high-flyer/140576".
    # Moka returns the job list AES-encrypted: body is {"data": <base64>, "necromancer": <key>}.
    # AES-128-CBC/PKCS7, key = necromancer (per response), IV = the page's bootstrap `aesIv` (static).
    org, site_id = board.split("/")
    # The aesIv is tenant-level but only rendered on a real board page, and the path
    # prefix varies by recruitment type (social/campus/intern), so try the known ones.
    iv = page_url = None
    for prefix in ("social-recruitment", "apply", "campus-recruitment", "campus_apply", "school"):
        page_url = f"https://app.mokahr.com/{prefix}/{org}/{site_id}"
        page = html.unescape(client.get(page_url, headers={"User-Agent": BROWSER_UA}).text)
        m = re.search(r'"aesIv"\s*:\s*"([0-9a-f]{16})"', page)
        if m:
            iv = m.group(1).encode()
            break
    if not iv:
        raise RuntimeError(f"moka: aesIv not found for {org}/{site_id}")

    headers = {
        "User-Agent": BROWSER_UA,
        "Referer": page_url,
        "Origin": "https://app.mokahr.com",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    jobs: list[Job] = []
    offset, limit = 0, 50
    while True:
        body = {"orgId": org, "siteId": int(site_id), "locale": "zh-CN", "offset": offset, "limit": limit}
        resp = post_json(
            client, "https://app.mokahr.com/api/outer/ats-apply/website/jobs/v2", headers=headers, json=body
        )
        key = resp["necromancer"].encode()
        plain = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(base64.b64decode(resp["data"])), 16)
        posts = (json.loads(plain).get("data") or {}).get("jobs") or []
        for j in posts:
            jid = str(j["id"])
            cities = _moka_locations(j.get("locations"))
            jobs.append(
                Job(
                    id=jid,
                    title=j["title"],
                    url=f"{page_url}/job/{jid}",
                    source="moka",
                    location=", ".join(cities) or None,
                    offices=cities,
                    date=j.get("createdAt"),
                    lastmod=j.get("updatedAt") or j.get("createdAt"),
                    body_html=j.get("jobDescription") or "",
                )
            )
        offset += limit
        if len(posts) < limit:
            break
    return jobs


def fetch_lever(client: httpx.Client, board: str) -> list[Job]:
    data = get_json(client, f"https://api.lever.co/v0/postings/{board}?mode=json")
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        body_parts = [j.get("description", "")]
        for lst in j.get("lists", []):
            body_parts.append(f"<h3>{lst.get('text', '')}</h3><ul>{lst.get('content', '')}</ul>")
        body_parts.append(j.get("additional", ""))
        created = _ms_to_iso(j.get("createdAt"))
        jobs.append(
            Job(
                id=str(j["id"]),
                title=j["text"],
                url=j.get("hostedUrl", ""),
                source="lever",
                location=cats.get("location"),
                departments=[cats["team"]] if cats.get("team") else [],
                offices=cats.get("allLocations") or [],
                date=created,
                lastmod=created,
                body_html="".join(p for p in body_parts if p),
            )
        )
    return jobs


def fetch_beisen(client: httpx.Client, board: str) -> list[Job]:
    # `board` is the tenant subdomain host, e.g. "iflytek.zhiye.com" (Beisen/北森 portal).
    # The tenant is resolved from the host, so no PageId is needed; the list response
    # already carries Duty/Require (full text), so no per-job detail call.
    origin = f"https://{board}"
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "xmlhttprequest",
        "langType": "zh_CN",
        "Referer": f"{origin}/social/jobs",
        "Origin": origin,
    }
    jobs: dict[str, Job] = {}
    for cat in ("1", "2"):  # 1 = social/社招, 2 = campus/校园
        index, size = 0, 50
        while True:
            body = {"Category": [cat], "PageIndex": index, "PageSize": size, "langType": "zh_CN"}
            data = post_json(client, f"{origin}/api/Jobad/GetJobAdPageList", headers=headers, json=body)
            posts = data.get("Data") or []
            for j in posts:
                jid = str(j["Id"])
                if jid in jobs:
                    continue
                cls = j.get("ClassificationOne")
                body_html = (
                    f"<h2>职位描述</h2>{_text_to_html(j.get('Duty'))}"
                    f"<h2>任职要求</h2>{_text_to_html(j.get('Require'))}"
                )
                # PostDate is usually the null sentinel "0001-...", ChangeDate carries the real value.
                def _dt(v: str | None) -> str | None:
                    return v if v and not v.startswith("0001") else None
                changed, posted = _dt(j.get("ChangeDate")), _dt(j.get("PostDate"))
                locs = j.get("LocNames") or []
                jobs[jid] = Job(
                    id=jid,
                    title=j["JobAdName"],
                    url=f"{origin}/jobs/{jid}",
                    source="beisen",
                    location=", ".join(locs) or None,
                    departments=[cls] if isinstance(cls, str) and cls else [],
                    offices=locs,
                    date=posted or changed,
                    lastmod=changed or posted,
                    body_html=body_html,
                )
            index += 1
            if len(posts) < size:
                break
    return list(jobs.values())


def fetch_unitree(client: httpx.Client, board: str) -> list[Job]:
    # Unitree's own first-party API (board arg unused; single endpoint). Full text in the list.
    origin = "https://www.unitree.com"
    headers = {"User-Agent": BROWSER_UA, "Origin": origin, "Referer": origin + "/"}
    data = get_json(client, "https://api.unitree.com/website/job/list?perPage=500", headers=headers)
    jobs = []
    for j in data.get("data", {}).get("items", []):
        jid = str(j["id"])
        body_html = f"<h2>岗位职责</h2>{_text_to_html(j.get('duty'))}<h2>任职要求</h2>{_text_to_html(j.get('ability'))}"
        jobs.append(
            Job(
                id=jid,
                title=j["title"],
                url=f"{origin}/cn/position/{jid}",
                source="unitree",
                location=j.get("cityId"),
                departments=[j["categoryId"]] if j.get("categoryId") else [],
                body_html=body_html,
            )
        )
    return jobs


def fetch_dahua(client: httpx.Client, board: str) -> list[Job]:
    # Dahua's Zhiye/智业 ATS (board arg unused). recruitType 1 and 2 cover campus + social;
    # the list response carries duty + require, so no per-job detail call.
    url = "https://job.dahuatech.com/talent-pool/api/bs-info/list-position-by-search"
    headers = {
        "User-Agent": BROWSER_UA,
        "Content-Type": "application/json",
        "syscode": "Recruit",
        "Referer": "https://job.dahuatech.com/",
        "Origin": "https://job.dahuatech.com",
    }
    # Dahua ignores pageNum/pageSize and returns the full set per recruitType in one call.
    jobs: dict[str, Job] = {}
    for recruit_type in (1, 2):
        data = post_json(client, url, headers=headers, json={"pageNum": 1, "pageSize": 1000, "recruitType": recruit_type})
        for j in data.get("data") or []:
            jid = str(j["jobAdId"])
            if jid in jobs:
                continue
            body_html = (
                f"<h2>职位描述</h2>{_text_to_html(j.get('duty'))}"
                f"<h2>任职要求</h2>{_text_to_html(j.get('require') or j.get('requirements'))}"
            )
            posted = j.get("postDate") or None
            jobs[jid] = Job(
                id=jid,
                title=j.get("jobAdName") or j.get("jobTitle"),
                url=f"https://job.dahuatech.com/post/{jid}",
                source="dahua",
                location=j.get("workingPlace"),
                departments=[j["jobCategroyDescription"]] if j.get("jobCategroyDescription") else [],
                date=posted,
                lastmod=posted,
                body_html=body_html,
            )
    return list(jobs.values())


def fetch_sensetime(client: httpx.Client, board: str) -> list[Job]:
    # `board` is the Dayee/大易 org id, e.g. "SU60fa3bdabef57c1023fc1cbc".
    # Form-urlencoded API under /wecruit/. The list lacks descriptions, so each job
    # needs a detail call (workContent + serviceCondition) — fanned out in parallel.
    org = board
    api = "https://hr.sensetime.com/wecruit/positionInfo"
    portal = f"https://hr.sensetime.com/{org}/pb/social.html"
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://hr.sensetime.com",
        "Referer": portal,
    }
    client.get(portal, headers={"User-Agent": BROWSER_UA})  # seed SERVERID cookie

    summaries: dict[str, tuple[dict, int]] = {}
    for rt in (2, 1):  # 2 = social/社招, 1 = campus/校园
        page, got = 1, 0
        while True:
            data = post_form(
                client, f"{api}/listPosition/{org}?request_locale=zh_CN", headers=headers,
                data={"isFrompb": "true", "recruitType": rt, "pageSize": 50, "currentPage": page},
            ).get("data") or {}
            posts = (data.get("pageForm") or {}).get("pageData") or []
            for j in posts:
                summaries.setdefault(str(j["postId"]), (j, rt))
            got += len(posts)
            page += 1
            if not posts or got >= (data.get("positonNum") or 0):
                break

    def detail(item: tuple[str, tuple[dict, int]]) -> tuple[str, dict]:
        post_id, (_, rt) = item
        try:
            d = post_form(
                client, f"{api}/listPositionDetail/{org}?request_locale=zh_CN", headers=headers,
                data={"postId": post_id, "recruitType": rt},
            ).get("data") or {}
            return post_id, d
        except httpx.HTTPError:
            return post_id, {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        details = dict(pool.map(detail, summaries.items()))

    jobs = []
    for post_id, (s, _) in summaries.items():
        d = details.get(post_id) or {}
        body_html = (
            f"<h2>工作内容</h2>{_text_to_html(d.get('workContent'))}"
            f"<h2>任职要求</h2>{_text_to_html(d.get('serviceCondition'))}"
        )
        published = (s.get("publishDate") or "").replace(" ", "T") or None
        jobs.append(
            Job(
                id=post_id,
                title=s["postName"],
                url=f"https://hr.sensetime.com/{org}/pb/posDetail.html?postId={post_id}&postType=society",
                source="sensetime",
                location=s.get("workPlaceStr"),
                departments=[s["department"]] if s.get("department") else [],
                date=published,
                lastmod=published,
                body_html=body_html,
            )
        )
    return jobs


def fetch_hikvision(client: httpx.Client, board: str) -> list[Job]:
    # `board` unused (single in-house board). List-only: getPostInfoForSys has no JD text,
    # but title/location/positionType/dates capture the hiring mix — the signal we want.
    # Descriptions would need a per-job auth-gated detail call (~1900/run); not worth it.
    url = "https://talent.hikvision.com/api/ats/official/officialPostPosition/getPostInfoForSys"
    headers = {
        "User-Agent": BROWSER_UA,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://talent.hikvision.com",
        "Referer": "https://talent.hikvision.com/society/index",
    }

    def _dt(v) -> str | None:
        if not v:
            return None
        if isinstance(v, (int, float)):
            return _ms_to_iso(int(v))
        return str(v).replace(" ", "T")

    jobs = []
    page, size = 1, 50
    while True:
        data = post_json(client, url, headers=headers, json={"pageNum": page, "pageSize": size, "companyId": ""}).get("data") or {}
        recs = data.get("list") or []
        for j in recs:
            sid = str(j["postSecureId"])
            ptype = j.get("positionType")
            jobs.append(
                Job(
                    id=sid,
                    title=j["postName"],
                    url=f"https://talent.hikvision.com/society/postDetail?postSecureId={sid}",
                    source="hikvision",
                    location=j.get("locationDesc"),
                    departments=[ptype] if ptype else [],
                    date=_dt(j.get("createTime")),
                    lastmod=_dt(j.get("updateTime")) or _dt(j.get("createTime")),
                    body_html="",
                )
            )
        page += 1
        if not recs or page > (data.get("pages") or 0):
            break
    return jobs


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "feishu": fetch_feishu,
    "moka": fetch_moka,
    "lever": fetch_lever,
    "beisen": fetch_beisen,
    "unitree": fetch_unitree,
    "dahua": fetch_dahua,
    "sensetime": fetch_sensetime,
    "hikvision": fetch_hikvision,
}


# --- writing / diffing ------------------------------------------------------

def _content_sig(text: str) -> str:
    # Ignore the volatile `date`/`lastmod` frontmatter lines when deciding if a posting
    # really changed, so a source bumping its timestamp doesn't manufacture a diff.
    return "\n".join(ln for ln in text.splitlines() if not re.match(r"^(date|lastmod): ", ln))


def write_if_changed(path: Path, content: str) -> str:
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if old == content:
            return "unchanged"
        # Only the timestamp moved (content identical) → keep the old file, no diff.
        if _content_sig(old) == _content_sig(content):
            return "unchanged"
        path.write_text(content, encoding="utf-8")
        return "updated"
    path.write_text(content, encoding="utf-8")
    return "new"


def archive_file(src: Path, dst: Path) -> None:
    """Move active -> archived, flipping status in the frontmatter."""
    text = src.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    if len(parts) == 3:
        front = yaml.safe_load(parts[1]) or {}
        front["status"] = "archived"
        text = "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n" + parts[2]
    dst.write_text(text, encoding="utf-8")
    src.unlink()


def sync_org(slug: str, jobs: list[Job], out: Path) -> str:
    active = out / slug / "active"
    archived = out / slug / "archived"
    active.mkdir(parents=True, exist_ok=True)
    archived.mkdir(parents=True, exist_ok=True)

    live_ids = {j.id for j in jobs}
    stats = {"new": 0, "updated": 0, "unchanged": 0, "reactivated": 0, "archived": 0}

    for job in jobs:
        path = active / f"{job.id}.md"
        was_archived = archived / f"{job.id}.md"
        if was_archived.exists():
            was_archived.unlink()
            stats["reactivated"] += 1
        stats[write_if_changed(path, job.to_markdown())] += 1

    for f in active.glob("*.md"):
        if f.stem not in live_ids:
            archive_file(f, archived / f.name)
            stats["archived"] += 1

    return (
        f"  {slug}: {len(jobs)} live "
        f"(+{stats['new']} new, ~{stats['updated']} updated, ={stats['unchanged']} same, "
        f"↩{stats['reactivated']} reactivated, →{stats['archived']} archived)"
    )


def run_source(client: httpx.Client, src: dict, out: Path) -> str:
    slug = src.get("slug") or slugify(src["name"])
    fetch = ADAPTERS.get(src["adapter"])
    if fetch is None:
        return f"  {slug}: no adapter for {src['adapter']!r}, skipping"
    try:
        jobs = fetch(client, src["board"])
        return sync_org(slug, jobs, out)
    except Exception as e:  # isolate per source — one bad board never aborts the run
        return f"  {slug}: failed ({type(e).__name__}: {e}), skipping"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", type=Path, default=Path("sources.json"))
    ap.add_argument("--out", type=Path, default=Path("content"))
    ap.add_argument("--only", help="slug to fetch (skip the rest)")
    ap.add_argument("--jobs", "-j", type=int, default=8, help="sources to fetch concurrently")
    args = ap.parse_args()

    sources = json.loads(args.sources.read_text())
    targets = [s for s in sources if not args.only or (s.get("slug") or slugify(s["name"])) == args.only]

    # httpx.Client is thread-safe; sources write to disjoint dirs, so run them concurrently.
    with httpx.Client(headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = [pool.submit(run_source, client, src, args.out) for src in targets]
            for fut in as_completed(futures):
                print(fut.result(), flush=True)


if __name__ == "__main__":
    main()

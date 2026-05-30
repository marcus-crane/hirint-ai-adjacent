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
        if self.departments:
            fm["departments"] = self.departments
        if self.offices:
            fm["offices"] = self.offices
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

def get_json(client: httpx.Client, url: str, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            return client.get(url).raise_for_status().json()
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
    page_url = f"https://app.mokahr.com/social-recruitment/{org}/{site_id}"
    page = html.unescape(client.get(page_url, headers={"User-Agent": BROWSER_UA}).text)
    m = re.search(r'"aesIv"\s*:\s*"([^"]+)"', page)
    if not m:
        raise RuntimeError(f"moka: aesIv not found in page for {org}")
    iv = m.group(1).encode()

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


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "feishu": fetch_feishu,
    "moka": fetch_moka,
    "lever": fetch_lever,
}


# --- writing / diffing ------------------------------------------------------

def write_if_changed(path: Path, content: str) -> str:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return "unchanged"
    existed = path.exists()
    path.write_text(content, encoding="utf-8")
    return "updated" if existed else "new"


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


def sync_org(slug: str, jobs: list[Job], out: Path) -> None:
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

    print(
        f"  {slug}: {len(jobs)} live "
        f"(+{stats['new']} new, ~{stats['updated']} updated, ={stats['unchanged']} same, "
        f"↩{stats['reactivated']} reactivated, →{stats['archived']} archived)"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", type=Path, default=Path("sources.json"))
    ap.add_argument("--out", type=Path, default=Path("content"))
    ap.add_argument("--only", help="slug to fetch (skip the rest)")
    args = ap.parse_args()

    sources = json.loads(args.sources.read_text())
    with httpx.Client(headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True) as client:
        for src in sources:
            slug = src.get("slug") or slugify(src["name"])
            if args.only and slug != args.only:
                continue
            fetch = ADAPTERS.get(src["adapter"])
            if fetch is None:
                print(f"  {slug}: no adapter for {src['adapter']!r}, skipping")
                continue
            try:
                jobs = fetch(client, src["board"])
            except httpx.HTTPError as e:
                print(f"  {slug}: fetch failed ({e!r}), skipping")
                continue
            sync_org(slug, jobs, args.out)


if __name__ == "__main__":
    main()

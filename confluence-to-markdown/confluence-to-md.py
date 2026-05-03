#!/usr/bin/env python3
"""
Confluence Space -> Markdown exporter

Uses Confluence Cloud REST API v2 (/wiki/api/v2): cursor pagination, pages, descendants, attachments.
See https://developer.atlassian.com/cloud/confluence/rest/v2/intro/#about

Requirements:
  pip install -r requirements.txt
  pandoc installed (preferred) or pypandoc

Run:
  python confluence-to-md.py --space <SPACE_KEY>
  python confluence-to-md.py --space <SPACE_KEY> --root <PAGE_ID>   # one page + descendants only
  python confluence-to-md.py --space KEY --request-delay 0.5 --max-retries 12   # gentler on rate limits

HTML is converted with pandoc to Markdown (not GFM): complex tables become Pandoc grid tables,
not raw HTML (the GFM writer leaves many tables as HTML).
"""

import argparse
import email.utils
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
import yaml
from bs4 import BeautifulSoup
from requests.auth import HTTPBasicAuth

# Attempt to import pypandoc
try:
    import pypandoc
except ImportError:
    pypandoc = None

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

class ConfluenceExporter:
    def __init__(
        self,
        base_url: str,
        space_key: str,
        username: str,
        token: str,
        output_dir: str,
        use_pypandoc: bool = False,
        root_page_id: Optional[str] = None,
        request_delay: float = 0.1,
        max_retries: int = 8,
        backoff_factor: float = 2.0,
        http_timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.space_key = space_key
        self.output_dir = output_dir
        self.use_pypandoc = use_pypandoc
        self.root_page_id = (root_page_id.strip() if root_page_id else None) or None

        # Throttling & retries (429 / transient 5xx)
        self.request_delay = max(0.0, float(request_delay))
        self.max_retries = max(0, int(max_retries))
        self.backoff_factor = max(1.0, float(backoff_factor))
        self.http_timeout = max(5.0, float(http_timeout))
        self.retry_status_codes = {429, 500, 502, 503, 504}
        self._max_retry_after_cap = 300.0

        # Configuration
        self.page_fetch_limit = 50

        # Session setup
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, token)
        self.session.headers.update({"Accept": "application/json"})

        # v2 base: {base_url}/api/v2 e.g. https://site.atlassian.net/wiki/api/v2
        self.api_v2_base = f"{self.base_url}/api/v2"
        self._space_id: Optional[str] = None

    @staticmethod
    def _nid(x) -> str:
        """Normalize Confluence content ids to str for consistent dict keys."""
        return str(x).strip() if x is not None else ""

    def ensure_dir(self, path: str):
        os.makedirs(path, exist_ok=True)

    def sanitize_filename(self, name: str) -> str:
        """Sanitize filename to prevent directory traversal and invalid characters."""
        name = name.strip()
        # Remove invalid chars
        name = re.sub(r'[\\/*?:"<>|]', "_", name)
        # Remove control chars
        name = "".join(c for c in name if c.isprintable())
        # Truncate
        return name[:200]

    def _throttle(self) -> None:
        """Pause between successful calls to reduce burst traffic."""
        if self.request_delay > 0:
            time.sleep(self.request_delay)

    @staticmethod
    def _retry_after_seconds(response: requests.Response) -> Optional[float]:
        """Parse ``Retry-After`` header (seconds or HTTP-date)."""
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        raw = raw.strip()
        if raw.isdigit():
            return float(min(int(raw), 86400))
        try:
            dt = email.utils.parsedate_to_datetime(raw)
            if dt is None:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            return None

    def _http_get(
        self,
        url: str,
        *,
        params: Optional[Dict] = None,
        stream: bool = False,
        timeout: Optional[float] = None,
        ok_without_raise: Tuple[int, ...] = (),
    ) -> requests.Response:
        """GET with retries on 429 / 5xx (selected) and ``Retry-After`` / exponential backoff."""
        to = timeout if timeout is not None else (max(self.http_timeout, 180.0) if stream else self.http_timeout)
        attempt = 0
        max_attempts = self.max_retries + 1

        while attempt < max_attempts:
            try:
                r = self.session.get(url, params=params, stream=stream, timeout=to)
            except requests.RequestException as e:
                attempt += 1
                if attempt >= max_attempts:
                    raise
                sleep_s = min(
                    self.request_delay * (self.backoff_factor ** (attempt - 1)),
                    self._max_retry_after_cap,
                )
                logger.warning(
                    "Request error for %s (attempt %s/%s), sleeping %.1fs: %s",
                    url,
                    attempt,
                    max_attempts,
                    sleep_s,
                    e,
                )
                time.sleep(sleep_s)
                continue

            if r.status_code in ok_without_raise:
                return r

            if r.status_code in self.retry_status_codes:
                attempt += 1
                if attempt >= max_attempts:
                    r.raise_for_status()
                ra = self._retry_after_seconds(r)
                if ra is not None:
                    sleep_s = min(max(ra, self.request_delay), self._max_retry_after_cap)
                else:
                    sleep_s = min(
                        self.request_delay * (self.backoff_factor ** (attempt - 1)),
                        self._max_retry_after_cap,
                    )
                logger.warning(
                    "HTTP %s for %s (attempt %s/%s), sleeping %.1fs",
                    r.status_code,
                    url,
                    attempt,
                    max_attempts,
                    sleep_s,
                )
                time.sleep(sleep_s)
                continue

            r.raise_for_status()
            return r

        raise RuntimeError("_http_get: exhausted retries without returning")

    def resolve_space_id(self) -> str:
        """Resolve space key to numeric space id (v2 uses space-id on /pages)."""
        if self._space_id:
            return self._space_id
        url = f"{self.api_v2_base}/spaces"
        params = {"keys": self.space_key, "limit": 10}
        try:
            r = self._http_get(url, params=params)
        except requests.RequestException as e:
            logger.error(f"Failed to resolve space key {self.space_key!r}: {e}")
            raise
        results = r.json().get("results", [])
        if not results:
            raise RuntimeError(f"No space found for key {self.space_key!r}")
        self._space_id = self._nid(results[0]["id"])
        logger.info(f"Space {self.space_key!r} -> id {self._space_id}")
        return self._space_id

    def _v2_follow_paginated_get(self, path: str, first_params: Optional[Dict] = None) -> List[Dict]:
        """GET {api_v2_base}{path} with cursor pagination via _links.next (v2)."""
        all_rows: List[Dict] = []
        url = f"{self.api_v2_base}{path}"
        params = dict(first_params) if first_params else {}
        while True:
            r = self._http_get(url, params=params or None)
            data = r.json()
            batch = data.get("results", [])
            all_rows.extend(batch)
            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            if next_link.startswith("http"):
                url = next_link
                params = {}
            else:
                url = urllib.parse.urljoin(self.base_url, next_link)
                params = {}
            self._throttle()
        return all_rows

    def fetch_all_pages(self) -> List[Dict]:
        """All pages in the space (minimal fields); ancestors added by enrich_ancestors_v2."""
        space_id = self.resolve_space_id()
        logger.info(f"Fetching page list for space: {self.space_key} (v2)")
        rows = self._v2_follow_paginated_get(
            "/pages",
            {"space-id": space_id, "limit": str(self.page_fetch_limit)},
        )
        for p in rows:
            p["id"] = self._nid(p["id"])
        logger.info(f"Fetched {len(rows)} page(s) total")
        return rows

    def _fetch_parent_node_meta(self, node_id: str, cache: Dict[str, Dict]) -> Dict:
        """Load title/parentId for a page or folder not present in the space page index."""
        if node_id in cache:
            return cache[node_id]
        for segment in ("pages", "folders"):
            u = f"{self.api_v2_base}/{segment}/{node_id}"
            try:
                r = self._http_get(u, ok_without_raise=(404,))
                if r.status_code == 404:
                    continue
                d = r.json()
                meta = {"title": d.get("title") or "", "parentId": d.get("parentId")}
                cache[node_id] = meta
                self._throttle()
                return meta
            except requests.RequestException:
                continue
        logger.warning(f"Could not resolve parent node {node_id} for folder path; using placeholder")
        meta = {"title": "", "parentId": None}
        cache[node_id] = meta
        return meta

    def _build_ancestors_v2(self, page: Dict, pages_by_id: Dict[str, Dict], meta_cache: Dict[str, Dict]) -> List[Dict]:
        """Ancestors from space root toward parent, [{id, title}, ...] — v1-compatible shape."""
        chain: List[Dict] = []
        parent_id = page.get("parentId")
        seen = set()
        while parent_id:
            pid = self._nid(parent_id)
            if pid in seen:
                break
            seen.add(pid)
            if pid in pages_by_id:
                node = pages_by_id[pid]
                chain.insert(0, {"id": pid, "title": node.get("title", "")})
                parent_id = node.get("parentId")
            else:
                m = self._fetch_parent_node_meta(pid, meta_cache)
                chain.insert(0, {"id": pid, "title": m.get("title", "")})
                parent_id = m.get("parentId")
        return chain

    def enrich_ancestors_v2(self, pages: List[Dict]) -> None:
        """Populate each page's 'ancestors' list for path and --root filtering."""
        pages_by_id = {p["id"]: p for p in pages}
        meta_cache: Dict[str, Dict] = {}
        for p in pages:
            p["ancestors"] = self._build_ancestors_v2(p, pages_by_id, meta_cache)

    def fetch_pages_subtree_v2(self, root_id: str) -> List[Dict]:
        """Root page plus all descendants (GET /pages/{id}/descendants), v2."""
        rid = self._nid(root_id)
        space_id = self.resolve_space_id()
        url = f"{self.api_v2_base}/pages/{rid}"
        logger.info(f"Fetching subtree via v2 descendants API (root page id={rid})")
        r = self._http_get(url)
        root = r.json()
        if self._nid(root.get("spaceId")) != space_id:
            logger.warning(
                "Root page spaceId does not match --space (export may be wrong). "
                f"expected {space_id}, got {root.get('spaceId')}"
            )
        root["id"] = self._nid(root["id"])
        pages_map: Dict[str, Dict] = {root["id"]: root}

        desc = self._v2_follow_paginated_get(
            f"/pages/{rid}/descendants",
            {"limit": str(self.page_fetch_limit)},
        )
        for row in desc:
            if row.get("type") != "page":
                continue
            row["id"] = self._nid(row["id"])
            pages_map[row["id"]] = row
        out = list(pages_map.values())
        logger.info(f"v2 subtree: {len(out)} page(s) (root + descendants)")
        return out

    @staticmethod
    def filter_pages_by_root(all_pages: List[Dict], root_id: str) -> List[Dict]:
        """Keep root page and any page that lists root_id in ancestors."""
        rkey = str(root_id).strip()
        out: List[Dict] = []
        for p in all_pages:
            if p["id"] == rkey:
                out.append(p)
                continue
            for a in p.get("ancestors", []):
                if str(a.get("id")) == rkey:
                    out.append(p)
                    break
        return out

    def resolve_pages_to_export(self) -> List[Dict]:
        """Full space, or subtree when --root is set (v2 descendants + fallback)."""
        if not self.root_page_id:
            pages = self.fetch_all_pages()
            self.enrich_ancestors_v2(pages)
            return pages

        try:
            pages = self.fetch_pages_subtree_v2(self.root_page_id)
        except requests.RequestException as e:
            logger.warning(f"v2 subtree fetch failed: {e}; falling back to full space list + filter")
            pages = []

        if not pages:
            all_pages = self.fetch_all_pages()
            self.enrich_ancestors_v2(all_pages)
            pages = self.filter_pages_by_root(all_pages, self.root_page_id)
            if not pages:
                logger.warning(
                    f"No pages found under root {self.root_page_id}. "
                    "Check the page id and space key."
                )
            else:
                logger.info(
                    f"Using {len(pages)} page(s) after filtering by root "
                    f"(full list had {len(all_pages)})"
                )
        else:
            self.enrich_ancestors_v2(pages)
        return pages

    def ancestor_titles_for_path(self, page_id: str, ancestors: List[Dict]) -> List[str]:
        """Titles used for folder segments (optionally stripped to paths under --root)."""
        pid = self._nid(page_id)
        rroot = self._nid(self.root_page_id) if self.root_page_id else ""

        if not self.root_page_id:
            return [self.sanitize_filename(a.get("title", "")) for a in ancestors if a.get("title")]

        if pid == rroot:
            return []

        ids = [self._nid(a.get("id")) for a in ancestors]
        try:
            idx = ids.index(rroot)
        except ValueError:
            return [self.sanitize_filename(a.get("title", "")) for a in ancestors if a.get("title")]

        below = ancestors[idx + 1 :]
        return [self.sanitize_filename(a.get("title", "")) for a in below if a.get("title")]

    def fetch_page_details(self, page_id: str) -> Dict:
        """GET /wiki/api/v2/pages/{id} with storage body and labels."""
        pid = self._nid(page_id)
        url = f"{self.api_v2_base}/pages/{pid}"
        params = {
            "body-format": "storage",
            "include-labels": "true",
        }
        try:
            r = self._http_get(url, params=params)
            return r.json()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch page details {pid}: {e}")
            raise

    @staticmethod
    def _storage_html(full: Dict) -> str:
        body = full.get("body") or {}
        storage = body.get("storage")
        if isinstance(storage, dict):
            return storage.get("value") or ""
        return ""

    @staticmethod
    def _labels_from_page_v2(full: Dict) -> List[str]:
        labels_block = full.get("labels") or {}
        results = labels_block.get("results") or []
        return [str(l.get("name")) for l in results if l.get("name")]

    def _resolve_attachment_download_url(self, att: Dict) -> Optional[str]:
        """Turn _links.download / downloadLink into a fetchable URL.

        Confluence Cloud often returns paths like ``/download/attachments/...``. Using
        ``urllib.parse.urljoin(wiki_base, '/download/...')`` incorrectly drops ``/wiki``
        and yields ``https://host/download/...`` (404). Same pattern as
        `confluence-markdown-exporter` (client.url + path).
        """
        links = att.get("_links") or {}
        dl_path = links.get("download") or att.get("downloadLink") or links.get("download")
        if not dl_path or not str(dl_path).strip():
            aid = self._nid(att.get("id"))
            if aid:
                return f"{self.base_url.rstrip('/')}/rest/api/content/{aid}/download"
            return None

        dl_path = str(dl_path).strip()
        if dl_path.startswith(("http://", "https://")):
            return dl_path

        base = self.base_url.rstrip("/")
        parsed = urllib.parse.urlparse(base)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        if not dl_path.startswith("/"):
            return urllib.parse.urljoin(base + "/", dl_path)

        # Paths already include /wiki (e.g. /wiki/download/...)
        if dl_path.startswith("/wiki/"):
            return origin + dl_path

        # Typical Cloud binary paths: /download/attachments/... live under the wiki webapp
        if dl_path.startswith("/download/"):
            return base + dl_path

        # REST API paths must include /wiki on standard Cloud URLs
        if dl_path.startswith("/rest/"):
            return base + dl_path

        return base + dl_path

    def fetch_attachments(self, page_id: str) -> List[Dict]:
        pid = self._nid(page_id)
        try:
            rows = self._v2_follow_paginated_get(
                f"/pages/{pid}/attachments",
                {"limit": "50"},
            )
        except requests.RequestException as e:
            logger.warning(f"v2 attachments failed for page {pid}: {e}")
            rows = []

        if not rows:
            rows = self._fetch_attachments_v1(pid)
            if rows:
                logger.debug(f"Using REST v1 attachment list for page {pid} ({len(rows)} file(s))")
        return rows

    def _fetch_attachments_v1(self, page_id: str) -> List[Dict]:
        """Fallback: GET /rest/api/content/{id}/child/attachment (same as atlassian-python-api)."""
        out: List[Dict] = []
        start = 0
        while True:
            url = f"{self.base_url}/rest/api/content/{page_id}/child/attachment"
            params = {"limit": 50, "start": start}
            try:
                r = self._http_get(url, params=params)
            except requests.RequestException as e:
                logger.warning(f"v1 attachment list failed for page {page_id}: {e}")
                break
            j = r.json()
            out.extend(j.get("results", []))
            if j.get("_links", {}).get("next"):
                start += 50
                self._throttle()
            else:
                break
        return out

    def download_attachment(self, att: Dict, save_dir: str) -> Optional[str]:
        full_url = self._resolve_attachment_download_url(att)
        if not full_url:
            logger.warning(
                "No download URL for attachment %s (keys: %s)",
                att.get("title"),
                list(att.keys()),
            )
            return None

        dl_path = (att.get("_links") or {}).get("download") or att.get("downloadLink") or ""
        filename = self.sanitize_filename(
            att.get("title") or os.path.basename(str(dl_path).split("?")[0]) or "attachment"
        )
        
        self.ensure_dir(save_dir)
        final_path = os.path.join(save_dir, filename)
        
        # Path traversal check
        if not os.path.abspath(final_path).startswith(os.path.abspath(save_dir)):
            logger.warning(f"Skipping attachment with unsafe filename: {filename}")
            return None

        try:
            dl_timeout = max(self.http_timeout, 300.0)
            r = self._http_get(full_url, stream=True, timeout=dl_timeout)
            with r:
                with open(final_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            return final_path
        except Exception as e:
            logger.error(f"Attachment download failed ({full_url}): {e}")
            return None

    def convert_html_to_markdown(self, html_text: str, dest_path: str):
        # Temp file
        tmp_html = dest_path + ".tmp.html"
        self.ensure_dir(os.path.dirname(dest_path))
        with open(tmp_html, "w", encoding="utf-8") as f:
            f.write(html_text)

        # Use pandoc "markdown" writer, not "gfm": gfm leaves complex tables as raw HTML.
        # Reader native_divs/native_spans avoids extra fenced div noise from wrapper fragments.
        pandoc_cmd = [
            "pandoc",
            tmp_html,
            "-f",
            "html-native_divs-native_spans",
            "-t",
            "markdown",
            "--markdown-headings=atx",
            "-o",
            dest_path,
        ]
        success = False
        try:
            subprocess.run(pandoc_cmd, check=True, capture_output=True)
            success = True
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            if isinstance(e, subprocess.CalledProcessError):
                err = (e.stderr or b"").decode("utf-8", errors="replace")
                logger.warning(f"pandoc CLI failed: {err[:500]}")
            # 2. Try pypandoc if enabled/available
            if self.use_pypandoc and pypandoc:
                try:
                    pypandoc.convert_text(
                        html_text,
                        "markdown",
                        format="html-native_divs-native_spans",
                        outputfile=dest_path,
                        extra_args=["--markdown-headings=atx"],
                    )
                    success = True
                except Exception as ex:
                    logger.warning(f"pypandoc conversion failed: {ex}")
            
        if not success:
            logger.warning(f"Pandoc unavailable/failed for {dest_path}. Falling back to strip-tags.")
            text = BeautifulSoup(html_text, "html.parser").get_text("\n\n")
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write(text)
        
        if os.path.exists(tmp_html):
            os.remove(tmp_html)

    def build_id_to_path_map(self, all_pages: List[Dict]) -> Dict[str, str]:
        mapping = {}
        # First pass: map ID to its simple title-based filename
        # Confluence ancestors list is usually complete if expand=ancestors was used
        
        for p in all_pages:
            pid = self._nid(p["id"])
            ancestors = p.get("ancestors", [])
            
            # Build relative directory path based on ancestors
            parts = self.ancestor_titles_for_path(pid, ancestors)
            title = self.sanitize_filename(p.get("title", f"page_{pid}"))
            
            rel_dir = os.path.join(*parts) if parts else ""
            rel_path = os.path.join(rel_dir, f"{title}.md")
            
            # Simple conflict resolution
            if rel_path in mapping.values():
                rel_path = os.path.join(rel_dir, f"{title}-{pid}.md")
            
            mapping[pid] = rel_path

        return mapping

    def rewrite_content(self, html_content: str, page_id: str, id_to_path: Dict[str, str], attachments_dirname: str) -> str:
        soup = BeautifulSoup(html_content, "html.parser")

        # 1. Images
        # <ac:image><ri:attachment ri:filename="..."/></ac:image>
        for ac_image in soup.find_all('ac:image'):
            ri = ac_image.find('ri:attachment')
            if ri and ri.get('ri:filename'):
                filename = self.sanitize_filename(ri['ri:filename'])
                new_img = soup.new_tag("img", src=os.path.join(attachments_dirname, filename))
                ac_image.replace_with(new_img)
            else:
                ri_url = ac_image.find('ri:url')
                if ri_url and ri_url.get('ri:value'):
                    new_img = soup.new_tag("img", src=ri_url['ri:value'])
                    ac_image.replace_with(new_img)

        # 2. Internal Page Links
        # <ac:link><ri:page ri:page-id="..."/></ac:link>
        for ac_link in soup.find_all('ac:link'):
            ri_page = ac_link.find('ri:page')
            if ri_page:
                target_pid = ri_page.get('ri:page-id') or ri_page.get('ri:content-title')
                target_pid = self._nid(target_pid) if target_pid else ""
                text = ac_link.get_text()
                
                if target_pid and target_pid in id_to_path:
                    # Calculate relative path from current page to target
                    curr_path = id_to_path.get(page_id)
                    target_path = id_to_path[target_pid]
                    
                    if curr_path:
                        rel = os.path.relpath(target_path, start=os.path.dirname(curr_path))
                    else:
                        rel = os.path.basename(target_path)
                        
                    if not text.strip():
                        # Fallback title if text empty
                        text = os.path.splitext(os.path.basename(target_path))[0]
                        
                    new_a = soup.new_tag("a", href=rel)
                    new_a.string = text
                    ac_link.replace_with(new_a)
                else:
                    ac_link.replace_with(text)

        # 3. Standard Links (href="/wiki/...")
        for a in soup.find_all('a', href=True):
            href = a['href']
            # Page ID links
            m_pid = re.search(r'pageId=(\d+)', href)
            if m_pid:
                target_pid = self._nid(m_pid.group(1))
                if target_pid in id_to_path:
                    curr_path = id_to_path.get(page_id)
                    target_path = id_to_path[target_pid]
                    rel = os.path.relpath(target_path, start=os.path.dirname(curr_path)) if curr_path else target_path
                    a['href'] = rel
            
            # Attachment links
            m_att = re.search(r'/download/attachments/[^/]+/([^/?#]+)', href)
            if m_att:
                filename = self.sanitize_filename(m_att.group(1))
                a['href'] = os.path.join(attachments_dirname, filename)

        # 4. Standard Images pointing to attachments
        for img in soup.find_all('img', src=True):
            src = img['src']
            m = re.search(r'/download/attachments/[^/]+/([^/?#]+)', src)
            if m:
                filename = self.sanitize_filename(m.group(1))
                img['src'] = os.path.join(attachments_dirname, filename)

        # 5. Clean Macros
        for macro in soup.find_all(re.compile(r'^ac:')):
            try:
                text = macro.get_text(separator=" ")
                macro.replace_with(text)
            except Exception:
                macro.decompose()
                
        return str(soup)

    def add_front_matter(self, md_path: str, meta: Dict):
        fm = {
            "title": meta.get("title"),
            "id": meta.get("id"),
            "labels": meta.get("labels", []),
            "version": meta.get("version"),
            # "created": meta.get("created"),
        }
        yaml_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
        
        with open(md_path, "r+", encoding="utf-8") as f:
            content = f.read()
            f.seek(0, 0)
            f.write("---\n" + yaml_text + "\n---\n\n" + content)

    def run(self):
        self.ensure_dir(self.output_dir)
        
        # 1. Fetch List (full space or subtree)
        all_pages = self.resolve_pages_to_export()
        logger.info(f"Total pages to process: {len(all_pages)}")
        
        # 2. Map IDs to Paths
        id_to_relpath = self.build_id_to_path_map(all_pages)
        
        # 3. Process Pages
        for idx, p in enumerate(all_pages, 1):
            pid = self._nid(p["id"])
            title = p.get("title", "Untitled")
            logger.info(f"[{idx}/{len(all_pages)}] Processing: {title} ({pid})")
            
            try:
                # Fetch full content (v2)
                full = self.fetch_page_details(pid)
                storage = self._storage_html(full)
                labels = self._labels_from_page_v2(full)
                
                # Paths
                rel_md_path = id_to_relpath.get(pid, f"{pid}.md")
                abs_md_path = os.path.join(self.output_dir, rel_md_path)
                page_dir_abs = os.path.dirname(abs_md_path)
                self.ensure_dir(page_dir_abs)
                
                # Attachments
                # We store attachments in a subfolder named "_attachments/{pid}" relative to the page's directory
                att_rel_dir = f"_attachments/{pid}" 
                att_abs_path = os.path.join(page_dir_abs, "_attachments", pid)
                
                page_attachments = self.fetch_attachments(pid)
                if page_attachments:
                    self.ensure_dir(att_abs_path)
                    for att in page_attachments:
                        self.download_attachment(att, att_abs_path)
                
                # Rewrite HTML (no outer <div>: native_divs reader handles fragments; a bare div
                # caused gfm to emit raw HTML around tables.)
                rewritten_html = self.rewrite_content(storage, pid, id_to_relpath, att_rel_dir)
                
                # Convert
                self.convert_html_to_markdown(rewritten_html, abs_md_path)
                
                # Front matter
                meta = {
                    "title": title,
                    "id": pid,
                    "labels": labels,
                    "version": full.get("version", {}).get("number"),
                }
                self.add_front_matter(abs_md_path, meta)
                
                self._throttle()
                
            except Exception as e:
                logger.error(f"Error processing page {pid} ({title}): {e}", exc_info=True)


def parse_args():
    parser = argparse.ArgumentParser(description="Export Confluence Space to Markdown")
    parser.add_argument("--url", default=os.getenv("CONFLUENCE_URL"), help="Confluence Base URL")
    parser.add_argument("--space", required=True, help="Space Key to export")
    parser.add_argument("--email", default=os.getenv("CONFLUENCE_EMAIL"), help="Auth Email (default: env CONFLUENCE_EMAIL)")
    parser.add_argument("--token", default=os.getenv("CONFLUENCE_API_TOKEN"), help="API Token (default: env CONFLUENCE_API_TOKEN)")
    parser.add_argument("--output", default="confluence_export", help="Output directory")
    parser.add_argument("--pypandoc", action="store_true", help="Use pypandoc library instead of pandoc CLI")
    parser.add_argument(
        "--root",
        metavar="PAGE_ID",
        default=None,
        help="Only export this page and its descendants (numeric id from the page URL). "
        "Paths are relative to this page in the output tree.",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=float(os.getenv("CONFLUENCE_REQUEST_DELAY", "0.1")),
        metavar="SECONDS",
        help="Pause after each successful page export and between paginated API chunks "
        "(default: 0.1 or CONFLUENCE_REQUEST_DELAY).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=int(os.getenv("CONFLUENCE_MAX_RETRIES", "8")),
        metavar="N",
        help="Max retries per HTTP request on 429 / 500 / 502 / 503 / 504 (default: 8, env CONFLUENCE_MAX_RETRIES).",
    )
    parser.add_argument(
        "--backoff-factor",
        type=float,
        default=float(os.getenv("CONFLUENCE_BACKOFF_FACTOR", "2")),
        metavar="MULT",
        help="Exponential backoff multiplier when Retry-After is absent (default: 2, env CONFLUENCE_BACKOFF_FACTOR).",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=float(os.getenv("CONFLUENCE_HTTP_TIMEOUT", "60")),
        metavar="SECONDS",
        help="Per-request timeout for JSON API calls; downloads use max(this, 300s). "
        "Default: 60 (env CONFLUENCE_HTTP_TIMEOUT).",
    )

    return parser.parse_args()

def main():
    args = parse_args()
    
    if not args.email or not args.token:
        # Check if running interactively or print help
        logger.error("Email and Token are required. Set via arguments or CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN env vars.")
        sys.exit(1)
        
    exporter = ConfluenceExporter(
        base_url=args.url,
        space_key=args.space,
        username=args.email,
        token=args.token,
        output_dir=args.output,
        use_pypandoc=args.pypandoc,
        root_page_id=args.root,
        request_delay=args.request_delay,
        max_retries=args.max_retries,
        backoff_factor=args.backoff_factor,
        http_timeout=args.http_timeout,
    )
    
    exporter.run()

if __name__ == "__main__":
    main()

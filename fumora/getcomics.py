"""GetComics (getcomics.org): our own indexer for its WordPress posts (M5).

A search page lists posts; a post holds one or more releases ("groups"): a line
with the release name and size, then one button per file host. Each group
becomes one release in the same shape as a Prowlarr result, so the decision
engine scores it like any other. The file is downloaded by `ddl.py`.

Ideas from Mylar3 (aio buttons, host order) and Kapowarr (button groups, redirects
to learn the real host); no code copied. Cloudflare: plain requests first; if
GetComics answers with a challenge and a FlareSolverr-compatible service (byparr)
is set, it solves the page and its cookies are reused.
"""

import asyncio
import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import httpx

DEFAULT_URL = "https://getcomics.org"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
# Hosts Fumora downloads from itself, best first. Others (Mega, MediaFire,
# TeraBox, VikingFile...) need site-specific clients and are skipped for now.
HOSTS = ("main", "pixeldrain")
MAX_POSTS = 8  # post pages read per search, to stay polite
PACE = 1.0  # seconds between requests to GetComics


class GetComicsError(Exception):
    pass


@dataclass
class Post:
    title: str
    url: str
    size_mb: float | None = None
    date: str | None = None  # YYYY-MM-DD


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _size_mb(text: str) -> float | None:
    m = re.search(r"Size\s*:\s*([\d.,]+)\s*([KMGT]B)", text, re.I)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return value * {"KB": 1 / 1024, "MB": 1, "GB": 1024, "TB": 1024 * 1024}[m.group(2).upper()]


def _host(label: str, url: str) -> str | None:
    label = label.strip().lower()
    netloc = urlsplit(url).netloc.lower()
    if "read online" in label:
        return None
    if label in ("download now", "main server", "main download", "direct download") or "comicfiles" in netloc:
        return "main"
    if "pixeldrain" in label or "pixeldrain" in netloc:
        return "pixeldrain"
    return re.sub(r"[^a-z0-9]+", "", label) or netloc or None


def parse_search(page: str) -> list[Post]:
    posts = []
    for art in re.findall(r"<article\b.*?</article>", page, re.S):
        m = re.search(r'<h1 class="post-title"><a href="([^"]+)"[^>]*>(.*?)</a>', art, re.S)
        if not m:
            continue
        excerpt = re.search(r'class="post-excerpt">(.*?)</p>\s*<ul', art, re.S)
        date = re.search(r'<time datetime="([\d-]{10})', art)
        posts.append(
            Post(
                title=_text(m.group(2)),
                url=m.group(1),
                size_mb=_size_mb(_text(excerpt.group(1))) if excerpt else None,
                date=date.group(1) if date else None,
            )
        )
    return posts


def _filename_title(links: list[tuple[str, str]]) -> str | None:
    """Some hosts' links end in the real file name, which names the scan group and
    source ("Absolute_Batman_024_(2026)_(Digital)_(Shan-Empire).cbz")."""
    for _host_name, url in links:
        last = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        if re.search(r"\.(cbz|cbr|cb7|zip|rar|pdf)$", last, re.I):
            return last.replace("_", " ")
    return None


def parse_post(page: str, post: Post) -> list[dict]:
    """The releases in a post, Prowlarr-shaped (protocol 'ddl', client 'getcomics')."""
    start = page.find('class="post-contents')
    body = page[start:] if start >= 0 else page
    end = body.find("<hr")
    body = body[: end if end > 0 else len(body)]
    year = re.search(r"Year\s*:\s*</strong>\s*((?:19|20)\d{2})", body)
    # A group starts at a centred paragraph with the release name and its size.
    headers = list(re.finditer(r"<p[^>]*>\s*<strong>(.*?)</strong>\s*<br\s*/?>(.*?)</p>", body, re.S | re.I))
    headers = [h for h in headers if "size" in h.group(2).lower()]
    spans = (
        [
            (h, h.end(), headers[n + 1].start() if n + 1 < len(headers) else len(body))
            for n, h in enumerate(headers)
        ]
        if headers
        else [(None, 0, len(body))]
    )
    releases = []
    for n, (h, a, b) in enumerate(spans):
        links = []
        for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", body[a:b], re.S):
            attrs = m.group(1)
            href = re.search(r'href="([^"]+)"', attrs)
            if not href:
                continue
            label = re.search(r'title="([^"]+)"', attrs)
            host = _host(label.group(1) if label else _text(m.group(2)), href.group(1))
            if host:
                links.append((host, html.unescape(href.group(1))))
        usable = sorted(((h_, u) for h_, u in links if h_ in HOSTS), key=lambda x: HOSTS.index(x[0]))
        if not usable:
            continue
        name = _text(h.group(1)) if h else post.title
        size = _size_mb(_text(h.group(2))) if h else post.size_mb
        title = _filename_title(links)
        if not title:
            title = name
            if year and year.group(1) not in title:
                title += f" ({year.group(1)})"
        releases.append(
            {
                "title": title,
                "size": int(size * 2**20) if size else None,
                "protocol": "ddl",
                "client": "getcomics",
                "indexer": "GetComics",
                "guid": f"{post.url}#{n}",
                "infoUrl": post.url,
                "publishDate": f"{post.date}T00:00:00Z" if post.date else None,
                # Read by ddl.py: the links to try, in order, and the post to re-read
                # if they have expired.
                "downloadUrl": json.dumps({"post": post.url, "group": n, "links": usable}),
            }
        )
    return releases


def query_text(query: str) -> str:
    """GetComics titles say '#24', not '024': drop zero padding."""
    return re.sub(r"(?<![\w.])0+(?=\d)", "", query).strip()


def is_challenge(resp: httpx.Response) -> bool:
    if resp.status_code not in (403, 429, 503):
        return False
    head = resp.text[:5000].lower()
    return "just a moment" in head or "cf-" in head or "challenge" in head or "cloudflare" in head


class GetComicsClient:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        flaresolverr_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        delay: float | None = None,
    ):
        self.base = (url or DEFAULT_URL).rstrip("/")
        self._solver = flaresolverr_url.rstrip("/") if flaresolverr_url else None
        self._delay = PACE if delay is None else delay
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            timeout=60.0, transport=transport, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )

    async def __aenter__(self) -> "GetComicsClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def _pace(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = self._last + self._delay - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = loop.time()

    async def get(self, url: str, params: dict | None = None) -> str:
        await self._pace()
        try:
            resp = await self._http.get(url, params=params)
        except httpx.TransportError as exc:
            raise GetComicsError(f"Could not reach GetComics ({type(exc).__name__})") from exc
        if is_challenge(resp):
            if not self._solver:
                raise GetComicsError(
                    "GetComics answered with a Cloudflare challenge; "
                    "set a FlareSolverr/byparr URL in Settings"
                )
            return await self._solve(str(resp.request.url))
        if resp.status_code == 404:
            return ""
        if resp.status_code >= 400:
            raise GetComicsError(f"GetComics returned HTTP {resp.status_code}")
        return resp.text

    async def _solve(self, url: str) -> str:
        """Fetch a page through FlareSolverr/byparr and keep its cookies."""
        try:
            resp = await self._http.post(
                f"{self._solver}/v1",
                json={"cmd": "request.get", "url": url, "maxTimeout": 60000},
                timeout=90.0,
            )
            data = resp.json()
        except (httpx.TransportError, ValueError) as exc:
            raise GetComicsError(f"FlareSolverr didn't answer ({type(exc).__name__})") from exc
        sol = data.get("solution") or {}
        if data.get("status") != "ok" or not sol.get("response"):
            raise GetComicsError(f"FlareSolverr couldn't open the page: {data.get('message') or 'no answer'}")
        for c in sol.get("cookies") or []:
            if c.get("name"):
                self._http.cookies.set(c["name"], c.get("value", ""), domain=c.get("domain") or "")
        if sol.get("userAgent"):
            self._http.headers["User-Agent"] = sol["userAgent"]
        return sol["response"]

    async def search_posts(self, query: str, page: int = 1) -> list[Post]:
        path = f"{self.base}/page/{page}/" if page > 1 else f"{self.base}/"
        return parse_search(await self.get(path, {"s": query_text(query)}))

    async def search(self, query: str, want: Callable[[str], bool]) -> list[dict]:
        """Releases from the posts `want` accepts by title (the caller's series and
        issues), so only the posts that matter are opened."""
        posts = [p for p in await self.search_posts(query) if want(p.title)][:MAX_POSTS]
        releases = []
        for p in posts:
            releases += parse_post(await self.get(p.url), p)
        return releases

    async def check(self) -> int:
        """Number of posts on the front page; raises GetComicsError if unusable."""
        posts = parse_search(await self.get(f"{self.base}/"))
        if not posts:
            raise GetComicsError("GetComics answered, but no posts were found (layout changed?)")
        return len(posts)

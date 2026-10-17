"""Prowlarr client: search every configured indexer at once.

Release download links contain the Prowlarr API key: they are kept server-side
and never shown in the UI or written to logs.
"""

import httpx

COMICS_CATEGORY = 7030  # Books/Comics


class ProwlarrError(Exception):
    pass


class ProwlarrClient:
    def __init__(self, url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=120.0,  # Prowlarr waits for every indexer
            transport=transport,
        )

    async def __aenter__(self) -> "ProwlarrClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def search(self, query: str, categories: tuple[int, ...] = (COMICS_CATEGORY,)) -> list[dict]:
        params = [("query", query), ("type", "search"), *(("categories", str(c)) for c in categories)]
        try:
            resp = await self._http.get("/api/v1/search", params=params)
        except httpx.TransportError as exc:
            raise ProwlarrError(f"Could not reach Prowlarr ({type(exc).__name__})") from exc
        if resp.status_code in (401, 403):
            raise ProwlarrError("Prowlarr rejected the API key")
        if resp.status_code >= 400:
            raise ProwlarrError(f"Prowlarr search returned HTTP {resp.status_code}")
        return resp.json()

    async def check(self) -> int:
        """Number of enabled indexers; raises ProwlarrError if Prowlarr isn't usable."""
        try:
            resp = await self._http.get("/api/v1/indexer")
        except httpx.TransportError as exc:
            raise ProwlarrError(f"Could not reach Prowlarr ({type(exc).__name__})") from exc
        if resp.status_code in (401, 403):
            raise ProwlarrError("Prowlarr rejected the API key")
        resp.raise_for_status()
        return sum(1 for i in resp.json() if i.get("enable"))

"""HTTP client untuk upstream PDDIKTI."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://pddikti.kemdiktisaintek.go.id/api"
DEFAULT_TIMEOUT = 8.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
ORIGIN = "https://pddikti.kemdiktisaintek.go.id"
REFERER = "https://pddikti.kemdiktisaintek.go.id/"

# Browser-like headers keep the upstream WAF from answering 403.
HEADERS = {
    "User-Agent": USER_AGENT,
    "Origin": ORIGIN,
    "Referer": REFERER,
    "Accept": "application/json, text/plain, */*",
}

# Fields the API emits. Anything else upstream sends is dropped.
SEARCH_FIELDS = ("id", "nama", "nim", "nama_pt", "sinkatan_pt", "nama_prodi")
DETAIL_FIELDS = (
    "id",
    "nama_pt",
    "kode_pt",
    "kode_prodi",
    "prodi",
    "nama",
    "nim",
    "jenis_daftar",
    "id_pt",
    "id_sms",
    "jenis_kelamin",
    "jenjang",
    "status_saat_ini",
    "tanggal_masuk",
)


class PddiktiError(Exception):
    """Base error: carries the HTTP status and the stable `code` slug."""

    status_code = 502
    code = "upstream_error"
    message = "PDDIKTI mengembalikan respons tidak valid"
    retry_after: str | None = None

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(f"{self.message}: {detail}" if (self.message and detail) else (detail or self.message))


class NotFound(PddiktiError):
    status_code, code = 404, "not_found"
    message = "mahasiswa tidak ditemukan di PDDIKTI"


class InvalidID(PddiktiError):
    status_code, code = 400, "invalid_id"
    message = "id tidak valid"


class InvalidPath(PddiktiError):
    status_code, code = 400, "invalid_path"
    message = "path upstream tidak valid"


class UpstreamRateLimited(PddiktiError):
    status_code, code = 429, "upstream_rate_limited"
    message = "rate limit PDDIKTI terlampaui (60 req/menit)"
    retry_after = "60"


class UpstreamBlocked(PddiktiError):
    status_code, code = 502, "upstream_blocked"
    message = "request ditolak WAF PDDIKTI (403 Forbidden)"


class UpstreamTimeout(PddiktiError):
    status_code, code = 504, "upstream_timeout"
    message = "PDDIKTI tidak merespons (timeout)"


class UpstreamError(PddiktiError):
    pass


class AmbiguousError(PddiktiError):
    """One NIM exactly matches students at more than one campus."""

    status_code, code = 409, "ambiguous_nim"
    message = "NIM terdaftar di lebih dari satu perguruan tinggi; sertakan parameter 'kampus'"

    def __init__(self, nim: str, candidates: list[dict[str, str]]) -> None:
        super().__init__()
        self.nim = nim
        self.candidates = candidates


class Client:
    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        cookie: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie if cookie is not None else _env("PDDIKTI_COOKIE")
        headers = dict(HEADERS)
        agent = user_agent if user_agent is not None else _env("PDDIKTI_USER_AGENT")
        if agent:
            headers["User-Agent"] = agent
        if self.cookie:
            headers["Cookie"] = self.cookie
        self._http = httpx.AsyncClient(timeout=timeout, headers=headers, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _fetch(
        self,
        path: str,
        raw_query: str = "",
        bad_request: type[PddiktiError] = NotFound,
        extra_headers: dict[str, str] | None = None,
    ) -> bytes:
        url = self.base_url + path + (f"?{raw_query}" if raw_query else "")
        try:
            resp = await self._http.get(url, headers=extra_headers or None)
        except httpx.TimeoutException:
            raise UpstreamTimeout() from None
        except httpx.HTTPError as exc:
            raise UpstreamError(str(exc)) from exc

        status = resp.status_code
        if status == 403:
            raise UpstreamBlocked()
        if status == 429:
            raise UpstreamRateLimited()
        if status in (400, 404):
            raise bad_request()
        if status != 200:
            raise UpstreamError(f"status {status}")
        return resp.content

    async def _get(self, path: str, bad_request: type[PddiktiError] = NotFound) -> dict[str, Any]:
        body = await self._fetch(path, bad_request=bad_request)
        try:
            data = json.loads(body)
        except ValueError:
            raise UpstreamError("gagal decode JSON") from None
        return data if isinstance(data, dict) else {}

    async def search(self, keyword: str) -> list[dict[str, Any]]:
        """Query students by NIM or name. Empty result is not an error."""
        clean = keyword.strip().replace("/", " ")  # slashes would change the route
        try:
            data = await self._get(f"/pencarian/mhs/{quote(clean, safe='')}")
        except NotFound:
            return []
        rows = data.get("data")
        return [_pick(row, SEARCH_FIELDS) for row in (rows if isinstance(rows, list) else [])]

    async def get_detail(self, encrypted_id: str) -> dict[str, Any]:
        encrypted_id = encrypted_id.strip()
        if not encrypted_id:
            raise InvalidID()
        data = await self._get(f"/detail/mhs/{quote(encrypted_id, safe='')}", InvalidID)
        detail = data.get("data")
        detail = detail if isinstance(detail, dict) else {}
        if not _str(detail.get("id")).strip() and not _str(detail.get("nim")).strip():
            raise NotFound()
        return _pick(detail, DETAIL_FIELDS)

    async def verify_nim(self, nim: str, campus: str = "") -> dict[str, Any]:
        """Find the student whose NIM matches exactly; never fall back to fuzzy."""
        clean = nim.strip()
        if not clean:
            raise NotFound()

        matches = [
            item
            for item in await self.search(clean)
            if _str(item.get("nim")).strip() == clean and _matches_campus(item, campus)
        ]
        if not matches:
            raise NotFound()
        if len(matches) > 1:
            raise AmbiguousError(
                clean,
                [
                    {
                        "nama_pt": _str(m.get("nama_pt")).strip(),
                        "singkatan_pt": _str(m.get("sinkatan_pt")).strip(),  # upstream spelling
                        "nama_prodi": _str(m.get("nama_prodi")).strip(),
                    }
                    for m in matches
                ],
            )

        detail = await self.get_detail(_str(matches[0].get("id")))
        # Defence in depth: the detail must belong to the NIM that was asked for.
        if _str(detail.get("nim")).strip() != clean:
            raise NotFound()
        return detail

    async def fetch(self, path: str, raw_query: str = "", extra_headers: dict[str, str] | None = None) -> bytes:
        """Mirror an upstream endpoint and return its JSON untouched."""
        if not valid_upstream_path(path):
            raise InvalidPath()
        return await self._fetch(path, raw_query, bad_request=NotFound, extra_headers=extra_headers)


def is_active(detail: dict[str, Any]) -> bool:
    """Current academic status starts with 'Aktif'."""
    return _str(detail.get("status_saat_ini")).strip().lower().startswith("aktif")


def valid_upstream_path(path: str) -> bool:
    """Reject anything that is not a plain absolute route."""
    if len(path) < 2 or not path.startswith("/") or "//" in path or "%2e" in path.lower():
        return False
    if any(seg in (".", "..") for seg in path.split("/")):
        return False
    return not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path)


def _matches_campus(item: dict[str, Any], campus: str) -> bool:
    campus = campus.strip().upper()
    if not campus:
        return True
    singkatan = _str(item.get("sinkatan_pt")).strip().upper()
    nama = _str(item.get("nama_pt")).strip().upper()
    return (singkatan != "" and singkatan == campus) or campus in nama


def _pick(item: Any, fields: tuple[str, ...]) -> dict[str, str]:
    """Keep the declared fields only, dropping everything else upstream sends."""
    row = item if isinstance(item, dict) else {}
    return {name: _str(row.get(name)) for name in fields}


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""
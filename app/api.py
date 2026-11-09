"""FastAPI routes, guards and error mapping (port of internal/server)."""

from __future__ import annotations

import hmac
import ipaddress
import json
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from . import pddikti

BULK_ROW_CAP = 50
IDLE_TTL = 10 * 60.0
SWEEP_EVERY = 60.0

# Upstream routes mirrored 1:1, plus the two bulk listings.
MIRRORED_PATHS = (
    "/pencarian/mhs/{q}",
    "/pencarian/dosen/{q}",
    "/pencarian/pt/{q}",
    "/pencarian/prodi/{q}",
    "/pencarian/enc/all/{q}",
    "/detail/mhs/{id}",
    "/dosen/portofolio/paten/{id}",
    "/dosen/study-history/{id}",
    "/dosen/teaching-history/{id}",
    "/prodi/cost-range/{id}",
    "/prodi/desc/{id}",
    "/prodi/detail/{id}",
    "/prodi/name-histories/{id}",
    "/prodi/num-students-lecturers/{id}",
    "/pt/cost-range/{id}",
    "/pt/detail/{id}",
    "/pt/graduation-rate/{id}",
    "/pt/jumlah-dosen/{id}",
    "/pt/jumlah-mahasiswa/{id}",
    "/pt/jumlah-prodi/{id}",
    "/pt/name-histories/{id}",
    "/pt/rasio/{id}",
    "/pt/waktu-studi/{id}",
)
BULK_PATHS = ("/v2/pt/search/filter", "/prodi/search/filter")


class BadRequest(pddikti.PddiktiError):
    status_code, code, message = 400, "bad_request", ""


class Unauthorized(pddikti.PddiktiError):
    status_code, code, message = 401, "unauthorized", "header X-API-Key tidak valid"


class RateLimitExceeded(pddikti.PddiktiError):
    status_code, code, message = 429, "rate_limited", "terlalu banyak request, coba lagi nanti"
    retry_after = "60"


@dataclass
class Config:
    """Protection settings. The zero value is valid: no key, any origin, no limit."""

    api_key: str = ""
    cors_origins: list[str] = field(default_factory=list)
    rate_per_minute: int = 0
    min_query_len: int = 0
    trusted_proxies: list[str] = field(default_factory=list)


class IPRateLimiter:
    """Per-IP token bucket. Idle entries are swept so memory stays bounded.

    State is per process, exactly like the Go service's in-process map: running
    several uvicorn workers gives each its own counter. The lock guards the
    threadpool path (a sync endpoint), not the event loop.
    """

    def __init__(self, per_minute: int, now: Callable[[], float] = time.monotonic) -> None:
        self.rate = per_minute / 60.0
        self.burst = float(per_minute)
        self._buckets: dict[str, list[float]] = {}  # ip -> [tokens, last_refill, last_seen]
        self._now = now
        self._last_sweep = 0.0
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        with self._lock:
            return self._allow(ip)

    def _allow(self, ip: str) -> bool:
        now = self._now()
        if now - self._last_sweep > SWEEP_EVERY:
            self._buckets = {k: v for k, v in self._buckets.items() if now - v[2] <= IDLE_TTL}
            self._last_sweep = now

        bucket = self._buckets.get(ip)
        if bucket is None:
            bucket = [self.burst, now, now]
            self._buckets[ip] = bucket
        else:
            bucket[0] = min(self.burst, bucket[0] + (now - bucket[1]) * self.rate)
            bucket[1] = now
        bucket[2] = now

        if bucket[0] < 1.0:
            return False
        bucket[0] -= 1.0
        return True


class StudentOut(BaseModel):
    nim: str
    nama: str
    kampus: str
    kode_pt: str
    prodi: str
    jenjang: str
    jenis_kelamin: str
    status_akademik: str
    tanggal_masuk: str


class CandidateOut(BaseModel):
    nama_pt: str
    singkatan_pt: str
    nama_prodi: str


class VerifyOut(BaseModel):
    status: str = "success"
    verified: bool = True
    is_active: bool
    eligible: bool
    student: StudentOut


class DetailOut(BaseModel):
    status: str = "success"
    data: dict[str, Any]


class SearchOut(BaseModel):
    status: str = "success"
    query: str
    total: int
    data: list[dict[str, Any]]


class HealthOut(BaseModel):
    status: str = "healthy"
    service: str = "auth-pddikti"
    time: str


def build_app(client: pddikti.Client, cfg: Config) -> FastAPI:
    limiter = IPRateLimiter(cfg.rate_per_minute) if cfg.rate_per_minute > 0 else None
    networks = _parse_networks(cfg.trusted_proxies)
    guard = _guard(cfg, limiter, networks)

    app = FastAPI(
        title="auth-pddikti",
        summary="Verifikasi identitas dan status aktif mahasiswa Indonesia berbasis PDDIKTI.",
        version="1.0.0",
        lifespan=_lifespan(client),
    )

    origin_list = [o.rstrip("/") for o in cfg.cors_origins]
    allow_all = not origin_list or "*" in origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if allow_all else origin_list,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-API-Key"],
    )

    @app.exception_handler(pddikti.PddiktiError)
    async def _error(_request: Request, exc: pddikti.PddiktiError) -> JSONResponse:
        body: dict[str, Any] = {"status": "error", "code": exc.code, "error": str(exc)}
        if isinstance(exc, pddikti.AmbiguousError):
            body["candidates"] = exc.candidates
        resp = JSONResponse(body, status_code=exc.status_code)
        if exc.retry_after:
            resp.headers["Retry-After"] = exc.retry_after
        return resp

    @app.get("/health", response_model=HealthOut)
    async def health() -> dict[str, str]:
        return {
            "status": "healthy",
            "service": "auth-pddikti",
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    api = APIRouter(prefix="/api", dependencies=[Depends(guard)] if guard else [])

    # Mirrored upstream routes first: /api/detail/mhs/{id} must win over /api/detail/{id}.
    for path in MIRRORED_PATHS:
        api.add_api_route(path, _mirror(client, 0), methods=["GET"])
    for path in BULK_PATHS:
        api.add_api_route(path, _mirror(client, BULK_ROW_CAP), methods=["GET"])

    async def verify(nim: str, campus: str) -> dict[str, Any]:
        nim = nim.strip()
        if not nim:
            raise BadRequest("NIM is required")
        return _verify_body(await client.verify_nim(nim, campus))

    @api.get("/search", response_model=SearchOut)
    async def search(q: str = "") -> dict[str, Any]:
        query = q.strip()
        if not query:
            raise BadRequest("Query parameter 'q' is required")
        if len(query) < cfg.min_query_len:
            raise BadRequest("Query parameter 'q' terlalu pendek")
        results = await client.search(query)
        return {"status": "success", "query": query, "total": len(results), "data": results}

    @api.get("/detail/{id}", response_model=DetailOut)
    async def detail(id: str) -> dict[str, Any]:
        return {"status": "success", "data": await client.get_detail(id)}

    @api.get("/verify/{nim}", response_model=VerifyOut)
    async def verify_get(nim: str, kampus: str = "") -> dict[str, Any]:
        return await verify(nim, kampus)

    @api.post("/verify", response_model=VerifyOut)
    async def verify_post(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("nim"), str):
            raise BadRequest("JSON body with field 'nim' is required")
        kampus = body.get("kampus")
        return await verify(body["nim"], kampus if isinstance(kampus, str) else "")

    app.include_router(api)
    return app


def cap_rows(raw: bytes, max_rows: int) -> bytes:
    """Truncate the row list of an upstream payload: a top-level array or obj["data"]."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(parsed, list):
        return json.dumps(parsed[:max_rows]).encode()
    if isinstance(parsed, dict):
        if isinstance(parsed.get("data"), list) and len(parsed["data"]) > max_rows:
            parsed["data"] = parsed["data"][:max_rows]
        return json.dumps(parsed).encode()
    return raw


def client_ip(request: Request, networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> str:
    """Connection IP. X-Forwarded-For is only believed when the peer is a trusted proxy."""
    host = request.client.host if request.client else ""
    if not networks or not _in_networks(host, networks):
        return host
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    for part in reversed(parts):
        if not _in_networks(part, networks):
            return part
    return host


def _mirror(client: pddikti.Client, row_cap: int) -> Callable[[Request], Awaitable[Response]]:
    async def handler(request: Request) -> Response:
        # raw_path keeps the client's own percent-encoding; /api is ours.
        raw = request.scope.get("raw_path")
        path = (raw.decode() if raw else request.url.path)[len("/api") :]
        body = await client.fetch(path, request.scope["query_string"].decode())
        if row_cap:
            body = cap_rows(body, row_cap)
        return Response(body, media_type="application/json; charset=utf-8")

    return handler


def _guard(cfg: Config, limiter: IPRateLimiter | None, networks: list[Any]) -> Callable[[Request], Awaitable[None]] | None:
    if not cfg.api_key and limiter is None:
        return None
    want = cfg.api_key.encode()

    async def guard(request: Request) -> None:
        if cfg.api_key and not hmac.compare_digest(request.headers.get("x-api-key", "").encode(), want):
            raise Unauthorized()
        if limiter is not None and not limiter.allow(client_ip(request, networks)):
            raise RateLimitExceeded()

    return guard


def _lifespan(client: pddikti.Client) -> Any:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await client.aclose()

    return lifespan


def _verify_body(detail: dict[str, Any]) -> dict[str, Any]:
    active = pddikti.is_active(detail)
    return {
        "status": "success",
        "verified": True,
        "is_active": active,
        # eligible is what callers should gate access on.
        "eligible": active,
        "student": {
            "nim": _str(detail.get("nim")),
            "nama": _str(detail.get("nama")),
            "kampus": _str(detail.get("nama_pt")),
            "kode_pt": _str(detail.get("kode_pt")).strip(),
            "prodi": _str(detail.get("prodi")),
            "jenjang": _str(detail.get("jenjang")),
            "jenis_kelamin": _str(detail.get("jenis_kelamin")),
            "status_akademik": _str(detail.get("status_saat_ini")),
            "tanggal_masuk": _str(detail.get("tanggal_masuk")),
        },
    }


def _parse_networks(cidrs: list[str]) -> list[Any]:
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr.strip(), strict=False))
        except ValueError:
            raise SystemExit(f"[-] TRUSTED_PROXIES tidak valid: {cidr!r}") from None
    return networks


def _in_networks(addr: str, networks: list[Any]) -> bool:
    try:
        parsed = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""
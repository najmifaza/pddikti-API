"""Parity checks for the FastAPI port. Run: python tests/test_api.py

No test framework: every test_* function is an assert-based self-check. All data
below is fictitious.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import api, pddikti  # noqa: E402


class StubClient:
    """Same shape as pddikti.Client, but answers from memory."""

    def __init__(self, search_res=None, detail=None, raw=None, err=None):
        self.search_res = search_res or []
        self.detail = detail
        self.raw = raw
        self.err = err
        self.got_nim = self.got_campus = self.got_path = self.got_query = None

    async def search(self, keyword):
        self.got_query = keyword
        if self.err:
            raise self.err
        return self.search_res

    async def get_detail(self, encrypted_id):
        if self.err:
            raise self.err
        return self.detail

    async def verify_nim(self, nim, campus=""):
        self.got_nim, self.got_campus = nim, campus
        if self.err:
            raise self.err
        return self.detail

    async def fetch(self, path, raw_query=""):
        self.got_path, self.got_query = path, raw_query
        if self.err:
            raise self.err
        return self.raw if self.raw is not None else b'{"status":"success","data":[]}'


def active_student(**over):
    detail = {
        "id": "id-a",
        "nim": "23010000001",
        "nama": "CONTOH MAHASISWA",
        "nama_pt": "Universitas Contoh",
        "kode_pt": "001001 ",
        "prodi": "Teknik Informatika",
        "jenjang": "Sarjana",
        "jenis_kelamin": "L",
        "status_saat_ini": "Aktif-2026/2027 Ganjil",
        "tanggal_masuk": "2023-08-15",
    }
    detail.update(over)
    return detail


def make_app(stub, **cfg):
    return api.build_app(stub, api.Config(**cfg))


def call(app, method, path, body=None, headers=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            return await c.request(method, path, content=body, headers=headers or {})

    return asyncio.run(go())


def body(resp):
    return json.loads(resp.content)


def test_health_and_docs_skip_the_api_key():
    app = make_app(StubClient(), api_key="secret")
    assert call(app, "GET", "/health").status_code == 200
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert call(app, "GET", path).status_code == 200, path
    spec = body(call(app, "GET", "/openapi.json"))
    assert spec["openapi"].startswith("3."), spec["openapi"]
    for path in (
        "/api/verify/{nim}",
        "/api/verify",
        "/api/search",
        "/api/detail/{id}",
        "/api/pencarian/mhs/{q}",
        "/api/pencarian/enc/all/{q}",
        "/api/pt/detail/{id}",
        "/api/prodi/search/filter",
        "/api/dosen/teaching-history/{id}",
    ):
        assert path in spec["paths"], path


def test_verify_success():
    stub = StubClient(detail=active_student())
    app = make_app(stub)
    resp = call(app, "GET", "/api/verify/23010000001?kampus=UC")
    assert resp.status_code == 200, resp.text
    data = body(resp)
    assert data["verified"] is data["is_active"] is data["eligible"] is True, data
    assert data["student"]["kode_pt"] == "001001", data["student"]["kode_pt"]
    assert stub.got_campus == "UC"

    resp = call(app, "POST", "/api/verify", json.dumps({"nim": "23010000001", "kampus": "Universitas Contoh"}))
    assert resp.status_code == 200, resp.text
    assert stub.got_campus == "Universitas Contoh"


def test_inactive_student_is_not_eligible():
    app = make_app(StubClient(detail=active_student(status_saat_ini="Dikeluarkan-2024/2025 Genap")))
    data = body(call(app, "GET", "/api/verify/23010000001"))
    assert data["verified"] is True
    assert data["is_active"] is False and data["eligible"] is False, data


def test_bad_requests():
    app = make_app(StubClient())
    for method, path, payload in (
        ("POST", "/api/verify", "{}"),
        ("POST", "/api/verify", "not json"),
        ("POST", "/api/verify", '{"nim":"   "}'),
        ("GET", "/api/verify/%20", None),
    ):
        resp = call(app, method, path, payload)
        assert resp.status_code == 400, (path, payload, resp.status_code)
        assert body(resp)["code"] == "bad_request", resp.text


def test_error_mapping():
    cases = [
        (pddikti.NotFound(), 404, "not_found"),
        (pddikti.InvalidID(), 400, "invalid_id"),
        (pddikti.InvalidPath(), 400, "invalid_path"),
        (pddikti.UpstreamRateLimited(), 429, "upstream_rate_limited"),
        (pddikti.UpstreamBlocked(), 502, "upstream_blocked"),
        (pddikti.UpstreamTimeout(), 504, "upstream_timeout"),
        (pddikti.UpstreamError("status 500"), 502, "upstream_error"),
    ]
    paths = (
        "/api/verify/123",
        "/api/detail/abc",
        "/api/search?q=abc",
        "/api/pencarian/dosen/budi",
        "/api/pt/detail/abc",
        "/api/v2/pt/search/filter?limit=1",
    )
    for err, status, slug in cases:
        app = make_app(StubClient(err=err))
        for path in paths:
            resp = call(app, "GET", path)
            assert resp.status_code == status, (err, path, resp.status_code)
            assert body(resp)["code"] == slug, (err, path, resp.text)


def test_ambiguous_nim():
    err = pddikti.AmbiguousError(
        "12345",
        [
            {"nama_pt": "UNIVERSITAS CONTOH", "singkatan_pt": "UC", "nama_prodi": "TEKNIK INFORMATIKA"},
            {"nama_pt": "POLITEKNIK CONTOH", "singkatan_pt": "PC", "nama_prodi": "TEKNIK MESIN"},
        ],
    )
    app = make_app(StubClient(err=err))
    resp = call(app, "GET", "/api/verify/12345")
    assert resp.status_code == 409, resp.text
    data = body(resp)
    assert data["code"] == "ambiguous_nim" and len(data["candidates"]) == 2, data


def test_search_min_query_len():
    stub = StubClient(search_res=[{"id": "x", "nim": "1"}])
    app = make_app(stub, min_query_len=3)
    assert call(app, "GET", "/api/search").status_code == 400
    assert call(app, "GET", "/api/search?q=%20ab%20").status_code == 400
    resp = call(app, "GET", "/api/search?q=abc")
    assert resp.status_code == 200 and body(resp)["total"] == 1, resp.text


def test_declared_fields_only():
    """Extra upstream fields must not leak: the Go structs decide the shape."""

    def handler(request):
        if "/pencarian/mhs/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "x",
                            "nim": "23010000001",
                            "nama": "CONTOH MAHASISWA",
                            "nama_pt": "Universitas Contoh",
                            "sinkatan_pt": "UC",
                            "nama_prodi": "TEKNIK INFORMATIKA",
                            "nim_asal": "bocor",
                            "extra": 1,
                        },
                        {"id": "y", "nim": "23010000002", "nama": 12345},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "x",
                    "nim": "23010000001",
                    "nama": "CONTOH MAHASISWA",
                    "nama_pt": "Universitas Contoh",
                    "kode_pt": "001001",
                    "kode_prodi": "55201",
                    "prodi": "TEKNIK INFORMATIKA",
                    "jenis_daftar": "UMUM",
                    "id_pt": "p1",
                    "id_sms": "s1",
                    "jenis_kelamin": "L",
                    "jenjang": "Sarjana",
                    "status_saat_ini": "Aktif-2026/2027 Ganjil",
                    "tanggal_masuk": "2023-08-15",
                    "nik": "bocor",
                    "extra": 1,
                }
            },
        )

    client = pddikti.Client(transport=httpx.MockTransport(handler))
    rows = asyncio.run(client.search("23010000001"))
    assert set(rows[0]) == set(pddikti.SEARCH_FIELDS), sorted(rows[0])
    assert rows[1]["nama"] == "", "non-string field must be coerced, not leaked"
    detail = asyncio.run(client.get_detail("x"))
    assert set(detail) == set(pddikti.DETAIL_FIELDS), sorted(detail)


def test_api_key():
    app = make_app(StubClient(detail=active_student()), api_key="secret")
    assert call(app, "GET", "/api/verify/1").status_code == 401
    assert call(app, "GET", "/api/verify/1", headers={"X-API-Key": "wrong"}).status_code == 401
    assert call(app, "GET", "/api/verify/1", headers={"X-API-Key": "secret"}).status_code == 200


def test_cors():
    # ponytail: Starlette answers preflight with 200 (and needs
    # Access-Control-Request-Method); the Go service used 204 for any OPTIONS.
    preflight = {"Origin": "https://a.example", "Access-Control-Request-Method": "GET"}
    resp = call(make_app(StubClient()), "OPTIONS", "/api/verify", headers=preflight)
    assert resp.status_code == 200, resp.status_code
    assert resp.headers["access-control-allow-origin"] == "*"

    app = make_app(StubClient(), cors_origins=["https://app.example/"])
    got = call(app, "OPTIONS", "/api/verify", headers={**preflight, "Origin": "https://app.example"})
    assert got.headers["access-control-allow-origin"] == "https://app.example"
    got = call(app, "OPTIONS", "/api/verify", headers={**preflight, "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in got.headers, got.headers


def test_rate_limit():
    app = make_app(StubClient(detail=active_student()), rate_per_minute=3)
    for i in range(3):
        assert call(app, "GET", "/api/verify/1").status_code == 200, f"request {i + 1}"
    resp = call(app, "GET", "/api/verify/1")
    assert resp.status_code == 429 and resp.headers.get("retry-after"), resp.text
    assert body(resp)["code"] == "rate_limited"

    # X-Forwarded-For must not bypass the limit when no proxy is trusted.
    spoofed = call(app, "GET", "/api/verify/1", headers={"X-Forwarded-For": "203.0.113.9"})
    assert spoofed.status_code == 429
    assert call(app, "GET", "/health").status_code == 200


def test_mirrored_route_passthrough():
    raw = b'{"status":"success","data":[{"id":"x","nama":"CONTOH DOSEN"}]}'
    stub = StubClient(raw=raw)
    app = make_app(stub)
    resp = call(app, "GET", "/api/pencarian/dosen/budi%20santoso?page=2&limit=5")
    assert resp.status_code == 200 and resp.content == raw, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert stub.got_path == "/pencarian/dosen/budi%20santoso", stub.got_path
    assert stub.got_query == "page=2&limit=5", stub.got_query

    # Legacy routes keep working next to the mirrored ones.
    legacy = make_app(StubClient(detail=active_student()))
    assert call(legacy, "GET", "/api/detail/mhs/id-a").status_code == 200
    assert call(legacy, "GET", "/api/verify/23010000001").status_code == 200


def test_unknown_paths_stay_404():
    stub = StubClient()
    app = make_app(stub)
    for path in (
        "/api/pencarian/guru/budi",
        "/api/pencarian/mhs",
        "/api/pt/unknown/x",
        "/api/dosen/study-history",
        "/api/v2/pt/search/other",
        "/api/detail/pt/x",
    ):
        assert call(app, "GET", path).status_code == 404, path
    assert stub.got_path is None, stub.got_path


def test_bulk_rows_are_capped():
    rows = [{"id": "x"} for _ in range(api.BULK_ROW_CAP + 25)]

    raw = json.dumps({"data": rows, "page": 1, "limit": 1000}).encode()
    data = body(call(make_app(StubClient(raw=raw)), "GET", "/api/v2/pt/search/filter?limit=1000"))
    assert len(data["data"]) == api.BULK_ROW_CAP, len(data["data"])

    raw = json.dumps(rows).encode()
    data = body(call(make_app(StubClient(raw=raw)), "GET", "/api/prodi/search/filter?q=informatika"))
    assert len(data) == api.BULK_ROW_CAP, len(data)

    short = b'{"status":"success","data":[{"id":"x"}]}'
    resp = call(make_app(StubClient(raw=short)), "GET", "/api/pencarian/mhs/23010000001")
    assert resp.content == short, "non-bulk route must not rewrite the payload"


def test_client_requires_exact_nim_match():
    def handler(request):
        return httpx.Response(200, json={"status": "success", "data": [{"id": "a", "nim": "23010000001"}]})

    client = pddikti.Client(transport=httpx.MockTransport(handler))
    for nim in ("2301000000", "230100000012", "30000001"):
        try:
            asyncio.run(client.verify_nim(nim))
        except pddikti.NotFound:
            continue
        raise AssertionError(f"partial NIM {nim} must not match")


def test_client_rechecks_detail_nim():
    def handler(request):
        if request.url.path.endswith("/pencarian/mhs/23010000001"):
            return httpx.Response(200, json={"data": [{"id": "a", "nim": "23010000001"}]})
        return httpx.Response(200, json={"data": {"id": "a", "nim": "99999999999"}})

    client = pddikti.Client(transport=httpx.MockTransport(handler))
    try:
        asyncio.run(client.verify_nim("23010000001"))
    except pddikti.NotFound:
        return
    raise AssertionError("detail for another NIM must be rejected")


def test_client_error_mapping():
    for status, expected in ((403, pddikti.UpstreamBlocked), (429, pddikti.UpstreamRateLimited), (500, pddikti.UpstreamError)):
        client = pddikti.Client(transport=httpx.MockTransport(lambda request, s=status: httpx.Response(s)))
        try:
            asyncio.run(client.search("budi"))
        except expected:
            continue
        raise AssertionError(f"status {status} must map to {expected.__name__}")

    def boom(request):
        raise httpx.TimeoutException("timeout")

    try:
        asyncio.run(pddikti.Client(transport=httpx.MockTransport(boom)).search("budi"))
    except pddikti.UpstreamTimeout:
        pass
    else:
        raise AssertionError("timeout must map to UpstreamTimeout")

    # Upstream 400/404 on search means "no result", not a failure.
    client = pddikti.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    assert asyncio.run(client.search("budi")) == []


def test_valid_upstream_path():
    for bad in ("", "/", "pencarian/mhs/x", "/pencarian//mhs", "/pencarian/../mhs", "/a/%2e%2e/b", "/a/\x01"):
        assert not pddikti.valid_upstream_path(bad), bad
    assert pddikti.valid_upstream_path("/pencarian/mhs/budi%20santoso")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            else:
                print(f"ok   {name}")
    raise SystemExit(1 if failures else 0)
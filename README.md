# auth-pddikti (FastAPI)

Microservice bridge dan HTTP client Python/FastAPI untuk verifikasi identitas dan status aktif mahasiswa Indonesia berbasis PDDIKTI (Kemdiktisaintek RI). Port 1:1 dari versi Go-nya (`akbarhann/auth-pddikti`): path, bentuk respons, kode error, dan variabel environment identik.

Layanan ini berfungsi sebagai lapisan verifikasi data (student verification / e-KYC layer) yang dirancang untuk melengkapi alur autentikasi aplikasi utama. Sistem memvalidasi keaktifan akademik, menarik identitas resmi (nama lengkap, NIM, perguruan tinggi, program studi, jenjang, status semester), dan menyediakan pencarian sivitas akademika secara on-demand tanpa scraping data massal.

## Struktur

| File | Isi |
|---|---|
| `app/pddikti.py` | client upstream (`httpx`), sentinel error, aturan exact-match NIM |
| `app/api.py` | route, guard API key + rate limit per IP, error mapping, mirror route, cap bulk |
| `app/main.py` | pembacaan env dan wiring aplikasi |
| `tests/test_api.py` | test parity, assert-based, tanpa dependency tambahan |

## Karakteristik Teknis

- On-demand single lookup: Menghindari beban scraping skala besar dengan mengeksekusi request langsung saat user mendaftar
- Role & eligibility verification: Memvalidasi keabsahan status mahasiswa aktif sebelum hak akses atau role diberikan oleh sistem utama
- Header compliance: Menyertakan header origin, referer, dan user-agent peramban untuk mencegah blokir HTTP 403 dari WAF upstream
- Deterministic match: Pencarian berbasis NIM memetakan satu mahasiswa spesifik secara presisi
- Cross-origin resource sharing: Mendukung integrasi langsung dari antarmuka web maupun mobile
- Cakupan nasional: Berlaku untuk seluruh perguruan tinggi negeri, swasta, dan politeknik di Indonesia

## Catatan Arsitektur

Layanan ini melakukan verifikasi data terhadap pangkalan data nasional secara read-only. Untuk menegakkan kepemilikan akun (identity ownership) secara mutlak, layanan ini direkomendasikan untuk dipasangkan bersama mekanisme autentikasi utama seperti Google Workspace OAuth institusi (`@kampus.ac.id`) atau verifikasi email kampus

## Persyaratan

- Python 3.10 atau lebih baru

## Menjalankan Layanan

```bash
git clone <url-repo-ini>
cd auth-pddikti-python
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8090
```

Di balik Nginx, teruskan `X-Forwarded-For` atau `X-Real-IP` dan isi `TRUSTED_PROXIES` agar rate limit membaca IP klien yang benar, bukan IP proxy.

### Konfigurasi

Semua variabel opsional. Contoh lengkap ada di [.env.example](.env.example).

| Variabel | Default | Keterangan |
|---|---|---|
| `PORT` | `8090` | Port HTTP |
| `API_KEY` | kosong | Jika diisi, semua endpoint `/api/*` wajib menyertakan header `X-API-Key` |
| `CORS_ORIGINS` | kosong (`*`) | Daftar origin yang diizinkan, dipisah koma |
| `RATE_LIMIT_PER_MINUTE` | `30` | Batas request `/api/*` per IP klien per menit (`0` = nonaktif) |
| `SEARCH_MIN_QUERY_LEN` | `3` | Panjang minimal parameter `q` pada `/api/search` |
| `TRUSTED_PROXIES` | kosong | IP/CIDR reverse proxy yang dipercaya untuk `X-Forwarded-For`. Isi jika layanan berada di balik proxy, agar rate limit membaca IP klien yang benar |

## Dokumentasi API

Dihasilkan otomatis oleh FastAPI, aktif selama layanan berjalan tanpa dependency tambahan:

| URL | Isi |
|---|---|
| `/docs` | Swagger UI interaktif |
| `/redoc` | ReDoc |
| `/openapi.json` | Spesifikasi OpenAPI 3.1 |

Halaman dokumentasi berada di luar `/api`, jadi tidak terkena `API_KEY` maupun rate limit.

## Testing

Test memakai upstream tiruan (`httpx.MockTransport` dan stub client) serta data fiktif, sehingga tidak mengirim request ke PDDIKTI:

```bash
python tests/test_api.py
```

Test ini menjaga parity dengan versi Go: exact-match NIM, re-check detail, kandidat ambigu, tabel error mapping, API key, rate limit, CORS, passthrough mirror route, whitelist path, dan cap baris listing bulk. Semua data uji fiktif; jangan pernah menaruh NIM atau nama mahasiswa asli di repo ini.

## Spesifikasi API

### 1. Verifikasi dan Pengambilan Data Mahasiswa (Berdasarkan NIM)

Mengambil data mahasiswa secara langsung dan mengembalikan status keaktifan semester berjalan.

- Method: GET atau POST
- Endpoint: `/api/verify/:nim` atau `/api/verify`

Contoh request GET:

```bash
curl -s http://localhost:8090/api/verify/23010000001
```

Contoh request POST:

```bash
curl -s -X POST http://localhost:8090/api/verify \
  -H "Content-Type: application/json" \
  -d '{"nim": "23010000001"}'
```

NIM harus cocok persis. NIM yang terpotong atau mirip tidak akan pernah dicocokkan ke mahasiswa lain. Karena NIM tidak unik secara nasional, sertakan parameter opsional `kampus` (nama atau singkatan perguruan tinggi, tidak case-sensitive) bila NIM yang sama terdaftar di lebih dari satu kampus:

```bash
curl -s "http://localhost:8090/api/verify/23010000001?kampus=UCI"
curl -s -X POST http://localhost:8090/api/verify \
  -H "Content-Type: application/json" \
  -d '{"nim": "23010000001", "kampus": "Universitas Contoh Indonesia"}'
```

Contoh response (200 OK):

```json
{
  "eligible": true,
  "is_active": true,
  "status": "success",
  "student": {
    "jenis_kelamin": "L",
    "jenjang": "Sarjana",
    "kampus": "Universitas Contoh Indonesia",
    "kode_pt": "001001",
    "nama": "CONTOH MAHASISWA",
    "nim": "23010000001",
    "prodi": "Teknik Informatika",
    "status_akademik": "Aktif-2026/2027 Ganjil",
    "tanggal_masuk": "2023-08-15"
  },
  "verified": true
}
```

> **Penting:** `verified: true` hanya berarti NIM ditemukan di PDDIKTI, termasuk mahasiswa yang sudah lulus, cuti, atau dikeluarkan. Gunakan `eligible` (bernilai `true` hanya jika status akademik `Aktif`) untuk memberikan hak akses atau role.

Contoh response bila NIM terdaftar di lebih dari satu kampus (409 Conflict):

```json
{
  "candidates": [
    { "nama_pt": "UNIVERSITAS CONTOH INDONESIA", "nama_prodi": "TEKNIK INFORMATIKA", "singkatan_pt": "UCI" },
    { "nama_pt": "POLITEKNIK CONTOH", "nama_prodi": "TEKNIK MESIN", "singkatan_pt": "PC" }
  ],
  "code": "ambiguous_nim",
  "error": "NIM terdaftar di lebih dari satu perguruan tinggi; sertakan parameter 'kampus'",
  "status": "error"
}
```

### 2. Pencarian Kandidat Mahasiswa (Berdasarkan Nama atau NIM)

Mengembalikan daftar mahasiswa yang cocok dengan kata kunci pencarian

- Method: GET
- Endpoint: `/api/search?q={keyword}`

Contoh request:

```bash
curl -s "http://localhost:8090/api/search?q=budi%20santoso"
```

Contoh response (200 OK):

```json
{
  "data": [
    {
      "id": "sample_encrypted_id_hash_value==",
      "nama": "BUDI SANTOSO",
      "nama_prodi": "TEKNIK INFORMATIKA",
      "nama_pt": "UNIVERSITAS CONTOH INDONESIA",
      "nim": "23010000001",
      "sinkatan_pt": "UCI"
    }
  ],
  "query": "budi santoso",
  "status": "success",
  "total": 1
}
```

### 3. Detail Mahasiswa Berdasarkan ID Terenkripsi

- Method: GET
- Endpoint: `/api/detail/:id`

Contoh request:

```bash
curl -s "http://localhost:8090/api/detail/sample_encrypted_id_hash_value=="
```

> **Catatan:** ID terenkripsi hanya berlaku untuk permintaan yang menghasilkannya. Nilai ID berbeda pada setiap pencarian, jadi pakai ID dari hasil pencarian langsung, jangan disimpan.

### 4. Verifikasi dan Pengambilan Data Dosen, Perguruan Tinggi, dan Program Studi

Seluruh endpoint di bawah ini memakai path, query string, dan bentuk respons yang sama dengan PDDIKTI — hanya ditambah prefiks `/api`. Respons diteruskan apa adanya, jadi struktur field mengikuti upstream.

Pencarian:

| Path | Isi |
|---|---|
| `/api/pencarian/mhs/{q}` | mahasiswa |
| `/api/pencarian/dosen/{q}` | dosen |
| `/api/pencarian/pt/{q}` | perguruan tinggi |
| `/api/pencarian/prodi/{q}` | program studi |
| `/api/pencarian/enc/all/{q}` | mahasiswa, dosen, PT, dan prodi sekaligus |

Contoh:

```bash
curl -s "http://localhost:8090/api/pencarian/dosen/budi%20santoso"
curl -s "http://localhost:8090/api/pencarian/enc/all/23010000001"
```

Detail, riwayat, dan statistik:

| Path | Isi |
|---|---|
| `/api/detail/mhs/{id}` | detail mahasiswa |
| `/api/pt/detail/{id}` | profil perguruan tinggi |
| `/api/pt/jumlah-mahasiswa/{id}` | jumlah mahasiswa |
| `/api/pt/jumlah-dosen/{id}` | jumlah dosen |
| `/api/pt/jumlah-prodi/{id}` | jumlah program studi |
| `/api/pt/rasio/{id}` | rasio dosen terhadap mahasiswa |
| `/api/pt/waktu-studi/{id}` | rata-rata masa studi per jenjang |
| `/api/pt/graduation-rate/{id}` | angka kelulusan |
| `/api/pt/cost-range/{id}` | kisaran biaya kuliah |
| `/api/pt/name-histories/{id}` | riwayat nama perguruan tinggi |
| `/api/prodi/detail/{id}` | profil program studi |
| `/api/prodi/desc/{id}` | ringkasan prodi: rasio, akreditasi, pendaftar, visi/misi |
| `/api/prodi/num-students-lecturers/{id}` | jumlah mahasiswa dan dosen per semester |
| `/api/prodi/cost-range/{id}` | kisaran biaya kuliah |
| `/api/prodi/name-histories/{id}` | riwayat nama program studi |
| `/api/dosen/study-history/{id}` | riwayat pendidikan dosen |
| `/api/dosen/teaching-history/{id}` | riwayat mengajar dosen |
| `/api/dosen/portofolio/paten/{id}` | portofolio paten dosen |

`{id}` di sini adalah ID terenkripsi dari hasil pencarian kategori terkait.

Listing besar:

| Path | Catatan |
|---|---|
| `/api/v2/pt/search/filter` | paginasi upstream dihormati |
| `/api/prodi/search/filter` | upstream mengabaikan `limit`; respons dipotong ke 50 baris |

Path di luar daftar ini tidak pernah diteruskan ke PDDIKTI dan dijawab `404`.

## Format Error

Semua error memakai bentuk `{"status": "error", "code": "<kode>", "error": "<pesan>"}`. Gunakan `code` untuk logika di aplikasi klien, karena isi `error` bisa berubah.

| HTTP | `code` | Arti |
|---|---|---|
| 400 | `bad_request` | Parameter tidak lengkap atau `q` terlalu pendek |
| 400 | `invalid_id` | ID terenkripsi tidak valid |
| 400 | `invalid_path` | Path upstream tidak valid |
| 401 | `unauthorized` | `API_KEY` aktif dan header `X-API-Key` salah atau tidak ada |
| 404 | `not_found` | Tidak ada mahasiswa dengan NIM yang cocok persis (dan kampus, bila diisi) |
| 409 | `ambiguous_nim` | NIM terdaftar di lebih dari satu kampus. Ulangi dengan parameter `kampus` |
| 429 | `rate_limited` | Batas request per IP pada layanan ini terlampaui |
| 429 | `upstream_rate_limited` | Batas 60 request/menit PDDIKTI terlampaui |
| 502 | `upstream_blocked` / `upstream_error` | PDDIKTI menolak request atau merespons tidak valid |
| 504 | `upstream_timeout` | PDDIKTI tidak merespons |

## Batasan Operasional Upstream

- Rate limit upstream PDDIKTI berada pada batas 60 request per menit per alamat IP publik
- Layanan ini diperuntukkan bagi verifikasi alur registrasi/autentikasi pengguna secara bertahap, bukan untuk penarikan data massal (bulk harvesting)

## Lisensi

Dirilis di bawah [MIT License](LICENSE).

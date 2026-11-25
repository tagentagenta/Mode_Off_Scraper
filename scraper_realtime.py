import os
import sys
import re
import html
import json
import time
import random
import threading
import subprocess
import webbrowser
import functools
import requests
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlparse, quote_plus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from curl_cffi import requests as cf_requests
from google import genai
from google.genai import types

# ================= KONFIGURASI SISTEM & ENV =================
PORT           = 8765
HTML_FILE      = "index.html"
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
FILE_RIWAYAT   = os.path.join(BASE_DIR, "loker_terkirim.json")
FILE_FEED_JSON = os.path.join(BASE_DIR, "loker_feed.json")
FILE_FEED_JS   = os.path.join(BASE_DIR, "loker_feed.js")
FILE_LOG       = os.path.join(BASE_DIR, "scraper.log")

def muat_env_lokal():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

muat_env_lokal()

GEMINI_API_KEY       = os.getenv("GEMINI_API_KEY", "")
GITHUB_TOKEN         = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO          = os.getenv("GITHUB_REPO", "")
GITHUB_WORKFLOW_FILE = os.getenv("GITHUB_WORKFLOW_FILE", "radar_loker.yml")
SUPABASE_URL         = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET_KEY  = os.getenv("SUPABASE_SECRET_KEY", "")

def tulis_log(pesan):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    baris = f"[{ts}] {pesan}"
    print(baris, flush=True)
    try:
        if os.path.exists(FILE_LOG) and os.path.getsize(FILE_LOG) > 1_000_000:
            os.remove(FILE_LOG)
        with open(FILE_LOG, "a", encoding="utf-8") as f:
            f.write(baris + "\n")
    except Exception:
        pass

MAX_LOKER_PER_RUN = 20
MAX_BATCH_FEED    = 30

KEYWORDS_JOBSTREET = [
    "Fresh Graduate Electrical",
    "Fresh Graduate Elektro",
    "Electrical Engineer",
    "Automation PLC",
    "Maintenance Electrical",
    "Embedded System",
    "Management Trainee Engineering",
]

KEYWORDS_GLINTS = [
    "Electrical Engineer",
    "Automation PLC",
    "Maintenance Electrical",
    "Embedded System",
    "Fresh Graduate Elektro",
]

KATA_TERLARANG_JUDUL = [
    "senior", "sr.", "manager", "mgr", "head", "lead", "director",
    "chief", "principal", "supervisor", "spv", "superintendent",
    "sales", "marketing", "accounting", "finance", "hrd", "legal"
]

POLA_PENGALAMAN_TINGGI = [
    re.compile(r"(?i)pengalaman\s+(?:kerja\s+)?(?:min(?:imal|\.)?\s*)?[2-9]\s*\+?\s*(?:tahun|thn)"),
    re.compile(r"(?i)min(?:imal|\.)?\s*[2-9]\s*\+?\s*(?:tahun|thn)\s+(?:pengalaman|di\s+bidang)"),
    re.compile(r"(?i)(?:at\s+least|minimum|min\.?)\s*[2-9]\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?experience"),
    re.compile(r"(?i)[2-9]\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:working\s+|relevant\s+)?experience"),
]

# ================= STATE SAKLAR & THREAD =================
state_lock = threading.Lock()
scan_lock = threading.Lock()
wake_event = threading.Event()

BOT_STATE = {
    "mode": "laptop_nyala",
    "auto_run": True,
    "is_scanning": False,
    "force_scan": False,
    "last_scan_at": "-",
    "next_run_at": "Menyiapkan...",
    "last_result": "Bot aktif (Jobstreet + Glints).",
    "cloud_status": "",
}


def cloud_terkonfigurasi():
    return bool(GITHUB_TOKEN.strip() and GITHUB_REPO.strip() and GITHUB_WORKFLOW_FILE.strip())


def atur_github_workflow(aktifkan: bool):
    if not cloud_terkonfigurasi():
        return False, "Mode Laptop Mati sedang dikembangkan."

    aksi = "enable" if aktifkan else "disable"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW_FILE}/{aksi}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        res = requests.put(url, headers=headers, timeout=15)
        if res.status_code in (200, 204):
            label = "AKTIF" if aktifkan else "NONAKTIF"
            return True, f"GitHub Actions {label}."
        return False, f"GitHub API HTTP {res.status_code}"
    except Exception as e:
        return False, f"Gagal hubungi GitHub: {e}"


def sinkron_git(arah="push"):
    if not os.path.isdir(os.path.join(BASE_DIR, ".git")):
        return
    try:
        if arah == "pull":
            subprocess.run(["git", "pull", "--rebase"], cwd=BASE_DIR, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["git", "add", "loker_terkirim.json"], cwd=BASE_DIR, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "commit", "-m", "Sync riwayat loker"], cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "push"], cwd=BASE_DIR, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def jelas_butuh_pengalaman_tinggi(teks):
    if not teks:
        return False
    return any(p.search(teks) for p in POLA_PENGALAMAN_TINGGI)


def buat_slug(teks):
    return re.sub(r"[^a-z0-9]+", "-", (teks or "lowongan").lower()).strip("-") or "lowongan"


# ================= HELPER PARSING HTML & DRAFT.JS =================
class JobAdDetailsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capturing = False
        self.depth = 0
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if not self.capturing and attrs_dict.get("data-automation") == "jobAdDetails":
            self.capturing = True
            self.depth = 1
            return
        if self.capturing:
            if tag not in ("br", "hr", "img", "input", "meta", "link"):
                self.depth += 1
            if tag == "li":
                self.chunks.append("\n• ")
            elif tag in ("p", "div", "br", "h1", "h2", "h3", "h4", "ul", "ol"):
                self.chunks.append("\n")

    def handle_startendtag(self, tag, attrs):
        if self.capturing and tag == "br":
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if self.capturing:
            if tag not in ("br", "hr", "img", "input", "meta", "link"):
                self.depth -= 1
            if tag in ("p", "div", "li", "h1", "h2", "h3", "h4"):
                self.chunks.append("\n")
            if self.depth <= 0:
                self.capturing = False

    def handle_data(self, data):
        if self.capturing:
            self.chunks.append(data)

    def get_text(self):
        teks = "".join(self.chunks)
        teks = re.sub(r"[ \t]+", " ", teks)
        teks = re.sub(r"\n\s*\n+", "\n\n", teks)
        return teks.strip()


def bersihkan_html(raw_html):
    if not raw_html:
        return ""
    teks = html.unescape(raw_html)
    teks = re.sub(r"(?i)<li[^>]*>", "\n• ", teks)
    teks = re.sub(r"(?i)</?(p|div|br|h[1-6]|ul|ol|tr)[^>]*>", "\n", teks)
    teks = re.sub(r"<[^>]+>", "", teks)
    teks = html.unescape(teks)
    teks = re.sub(r"[ \t]+", " ", teks)
    teks = re.sub(r"\n\s*\n+", "\n\n", teks)
    return teks.strip()


def ekstrak_ld_json_jobdesc(html_page):
    ld_matches = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_page,
        flags=re.DOTALL | re.IGNORECASE,
    )
    for blok in ld_matches:
        try:
            obj = json.loads(blok.strip())
            items = obj if isinstance(obj, list) else [obj]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "JobPosting" and item.get("description"):
                    desc_bersih = bersihkan_html(item["description"])
                    if len(desc_bersih) > 80:
                        return desc_bersih
        except Exception:
            continue
    return ""


def parse_draftjs_json(raw_str):
    if not raw_str:
        return ""
    try:
        obj = json.loads(raw_str) if isinstance(raw_str, str) else raw_str
        blocks = obj.get("blocks", []) if isinstance(obj, dict) else []
        lines = []
        for b in blocks:
            txt = (b.get("text") or "").strip()
            if not txt:
                continue
            if b.get("type") in ("unordered-list-item", "ordered-list-item"):
                lines.append(f"• {txt}")
            else:
                lines.append(txt)
        return "\n".join(lines).strip()
    except Exception:
        return bersihkan_html(str(raw_str))


def ambil_detail_jobstreet(session, job_id, fallback_text):
    url_detail = f"https://id.jobstreet.com/id/job/{job_id}"
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://id.jobstreet.com/id",
    }
    try:
        res = session.get(url_detail, headers=headers, timeout=15)
        if res.status_code != 200:
            return fallback_text

        desc_ld = ekstrak_ld_json_jobdesc(res.text)
        if desc_ld:
            return desc_ld

        parser = JobAdDetailsParser()
        parser.feed(res.text)
        desc_html = parser.get_text()
        if len(desc_html) > 80:
            return desc_html
    except Exception:
        pass

    return fallback_text


def ambil_detail_glints(session, url_detail, fallback_text):
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://glints.com/id/opportunities/jobs/explore",
    }
    try:
        res = session.get(url_detail, headers=headers, timeout=15)
        if res.status_code != 200:
            return fallback_text

        html_page = res.text

        # Cara 1: JSON-LD JobPosting
        desc_ld = ekstrak_ld_json_jobdesc(html_page)
        if desc_ld:
            return desc_ld

        # Cara 2: Next.js __NEXT_DATA__ (descriptionJsonString / description)
        m = re.search(
            r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html_page,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if m:
            next_data = json.loads(m.group(1))
            hasil_teks = []

            def cari_desc(node):
                if isinstance(node, dict):
                    if node.get("descriptionJsonString"):
                        teks = parse_draftjs_json(node["descriptionJsonString"])
                        if len(teks) > 80:
                            hasil_teks.append(teks)
                    elif isinstance(node.get("description"), str) and len(node["description"]) > 120:
                        teks = bersihkan_html(node["description"])
                        if len(teks) > 80:
                            hasil_teks.append(teks)
                    for v in node.values():
                        cari_desc(v)
                elif isinstance(node, list):
                    for item in node:
                        cari_desc(item)

            cari_desc(next_data)
            if hasil_teks:
                return max(hasil_teks, key=len)
    except Exception:
        pass

    return fallback_text


# ================= MEMORI & SINKRONISASI FEED =================
def supabase_aktif():
    return bool(SUPABASE_URL and SUPABASE_SECRET_KEY)

def sb_headers(extra=None):
    h = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h

def muat_riwayat():
    riwayat = set()
    if os.path.exists(FILE_RIWAYAT):
        try:
            with open(FILE_RIWAYAT, "r", encoding="utf-8") as f:
                riwayat.update(json.load(f))
        except Exception:
            pass

    if supabase_aktif():
        try:
            res = requests.get(
                f"{SUPABASE_URL}/rest/v1/loker_seen?select=job_id&limit=5000",
                headers=sb_headers(),
                timeout=15,
            )
            if res.ok:
                for row in res.json():
                    if row.get("job_id"):
                        riwayat.add(str(row["job_id"]))
        except Exception as e:
            tulis_log(f"[SUPABASE] Gagal muat loker_seen: {e}")

    return riwayat

def simpan_riwayat(riwayat_set, id_baru=None):
    with open(FILE_RIWAYAT, "w", encoding="utf-8") as f:
        json.dump(sorted(list(riwayat_set)), f, indent=2)

    if supabase_aktif():
        daftar_sync = list(id_baru) if id_baru else list(riwayat_set)
        if not daftar_sync:
            return
        try:
            rows = [{"job_id": str(jid)} for jid in daftar_sync]
            requests.post(
                f"{SUPABASE_URL}/rest/v1/loker_seen",
                headers=sb_headers({"Prefer": "resolution=ignore-duplicates"}),
                json=rows,
                timeout=15,
            )
        except Exception as e:
            tulis_log(f"[SUPABASE] Gagal simpan loker_seen: {e}")

def pastikan_file_feed_ada():
    if not os.path.exists(FILE_FEED_JS):
        with open(FILE_FEED_JS, "w", encoding="utf-8") as f:
            f.write("window.GENTA_LOKER_FEED = [];\n")

def simpan_ke_feed_dashboard(loker_lolos, source="Jobstreet"):
    batches = []
    if os.path.exists(FILE_FEED_JSON):
        try:
            with open(FILE_FEED_JSON, "r", encoding="utf-8") as f:
                batches = json.load(f)
        except Exception:
            batches = []

    batch_baru = {
        "batch_id": f"{source.lower()}_{int(time.time() * 1000)}",
        "source": source,
        "waktu": datetime.now().strftime("%d %b %Y, %H:%M WIB"),
        "items": loker_lolos,
    }
    batches.append(batch_baru)
    batches = batches[-50:]

    with open(FILE_FEED_JSON, "w", encoding="utf-8") as f:
        json.dump(batches, f, ensure_ascii=False, indent=2)

    with open(FILE_FEED_JS, "w", encoding="utf-8") as f:
        f.write("window.GENTA_LOKER_FEED = ")
        json.dump(batches, f, ensure_ascii=False)
        f.write(";\n")

    if supabase_aktif():
        try:
            res = requests.post(
                f"{SUPABASE_URL}/rest/v1/loker_batches",
                headers=sb_headers({"Prefer": "resolution=merge-duplicates"}),
                json=batch_baru,
                timeout=15,
            )
            if not res.ok:
                tulis_log(f"[SUPABASE] Gagal push loker_batches: HTTP {res.status_code} {res.text[:120]}")
        except Exception as e:
            tulis_log(f"[SUPABASE] Error push loker_batches: {e}")


# ================= SCRAPING JOBSTREET =================
def ambil_loker_jobstreet(session, keyword, jumlah=8):
    url = "https://id.jobstreet.com/api/jobsearch/v5/search"
    params = {
        "siteKey": "ID-Main",
        "sourcesystem": "houston",
        "page": 1,
        "pageSize": jumlah,
        "keywords": keyword,
        "sortmode": "ListedDate",
        "locale": "id-ID",
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://id.jobstreet.com/id",
        "Origin": "https://id.jobstreet.com",
    }

    res = session.get(url, params=params, headers=headers, timeout=20)
    if res.status_code == 403:
        raise RuntimeError("Akses Jobstreet ditolak Cloudflare (HTTP 403).")
    res.raise_for_status()
    return res.json().get("data", [])


# ================= SCRAPING GLINTS INDONESIA =================
def format_gaji_glints(salaries):
    if not salaries or not isinstance(salaries, list):
        return "Tidak dicantumkan"

    for s in salaries:
        if not isinstance(s, dict):
            continue
        min_amt = s.get("minAmount") or s.get("min") or 0
        max_amt = s.get("maxAmount") or s.get("max") or 0
        curr = (s.get("CurrencyCode") or s.get("currencyCode") or "IDR").upper()
        prefix = "Rp " if curr == "IDR" else f"{curr} "

        if min_amt and max_amt:
            return f"{prefix}{int(min_amt):,} - {prefix}{int(max_amt):,}".replace(",", ".")
        elif min_amt:
            return f"Mulai {prefix}{int(min_amt):,}".replace(",", ".")
        elif max_amt:
            return f"Hingga {prefix}{int(max_amt):,}".replace(",", ".")

    return "Tidak dicantumkan"


def ekstrak_lokasi_glints(job_dict):
    bagian = []
    for key in ("city", "location", "province"):
        val = job_dict.get(key)
        if isinstance(val, dict):
            nama = val.get("name") or val.get("formattedName")
            if nama and nama not in bagian:
                bagian.append(nama)
            for parent_key in ("parents", "parent"):
                p = val.get(parent_key)
                if isinstance(p, dict) and p.get("name") and p["name"] not in bagian:
                    bagian.append(p["name"])
                elif isinstance(p, list):
                    for item in p[:2]:
                        if isinstance(item, dict) and item.get("name") and item["name"] not in bagian:
                            bagian.append(item["name"])
        elif isinstance(val, str) and val.strip() and val not in bagian:
            bagian.append(val.strip())

    return ", ".join(bagian[:2]) if bagian else "Indonesia"


def ekstrak_jobs_glints_dari_nextdata(next_data):
    """Mencari daftar lowongan secara rekursif dari __NEXT_DATA__ Glints agar tahan perubahan struktur."""
    ditemukan = []
    seen_ids = set()

    def telusuri(node):
        if isinstance(node, dict):
            jid = node.get("id")
            title = node.get("title")
            company_obj = node.get("company")
            if (
                isinstance(jid, str)
                and len(jid) >= 8
                and isinstance(title, str)
                and title.strip()
                and isinstance(company_obj, dict)
                and company_obj.get("name")
                and jid not in seen_ids
            ):
                seen_ids.add(jid)
                ditemukan.append(node)
            for v in node.values():
                telusuri(v)
        elif isinstance(node, list):
            for item in node:
                telusuri(item)

    telusuri(next_data)
    return ditemukan


def ambil_loker_glints(session, keyword, jumlah=8):
    url_explore = (
        f"https://glints.com/id/opportunities/jobs/explore"
        f"?keyword={quote_plus(keyword)}&country=ID&locationName=All+Cities&sortBy=LATEST"
    )
    headers_html = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://glints.com/id",
    }

    try:
        res = session.get(url_explore, headers=headers_html, timeout=20)
        if res.status_code == 200:
            m = re.search(
                r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                res.text,
                flags=re.DOTALL | re.IGNORECASE,
            )
            if m:
                next_data = json.loads(m.group(1))
                jobs = ekstrak_jobs_glints_dari_nextdata(next_data)
                if jobs:
                    return jobs[:jumlah]
            else:
                tulis_log(f"[GLINTS SSR] __NEXT_DATA__ tidak ditemukan untuk '{keyword}' (mungkin berubah ke App Router/blocked)")
        else:
            tulis_log(f"[GLINTS SSR] HTTP {res.status_code} untuk '{keyword}'")
    except Exception as e:
        tulis_log(f"[GLINTS SSR ERROR] {keyword}: {e}")

    # Cara 2 (Fallback): GraphQL API Glints
    gql_url = "https://glints.com/api/v2-alc/graphql"
    gql_query = """
    query searchJobsV3($data: SearchJobsInput!) {
      searchJobsV3(data: $data) {
        jobsInPage {
          id
          title
          minYearsOfExperience
          maxYearsOfExperience
          updatedAt
          createdAt
          company { name }
          city { name }
          salaries { minAmount maxAmount CurrencyCode }
        }
      }
    }
    """
    payload = {
        "operationName": "searchJobsV3",
        "variables": {
            "data": {
                "SearchTerm": keyword,
                "CountryCode": "ID",
                "limit": jumlah,
                "offset": 0,
                "includeExternalJobs": True,
                "sources": ["NATIVE", "SUPER_POWERED"],
            }
        },
        "query": gql_query,
    }
    headers_gql = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://glints.com",
        "Referer": "https://glints.com/id/opportunities/jobs/explore",
        "x-glints-country-code": "ID",
    }
    try:
        res_gql = session.post(gql_url, json=payload, headers=headers_gql, timeout=20)
        if res_gql.status_code == 200:
            data_gql = res_gql.json()
            if data_gql.get("errors"):
                tulis_log(f"[GLINTS GQL ERRORS] {data_gql['errors'][0].get('message')}")
            return (
                data_gql.get("data", {})
                .get("searchJobsV3", {})
                .get("jobsInPage", [])[:jumlah]
            )
        else:
            tulis_log(f"[GLINTS GQL] HTTP {res_gql.status_code} untuk '{keyword}'")
    except Exception as e:
        tulis_log(f"[GLINTS GQL ERROR] {keyword}: {e}")

    return []


# ================= BATCH AI SCREENING =================
def screening_batch_ai(client, daftar_kandidat):
    blok_loker = []
    for k in daftar_kandidat:
        blok_loker.append(
            f"[ID: {k['id']}]\n"
            f"Sumber: {k['source']}\n"
            f"Judul: {k['title']}\n"
            f"Perusahaan: {k['company']}\n"
            f"Lokasi: {k['location']}\n"
            f"Gaji: {k['salary']}\n"
            f"Deskripsi & Syarat:\n{k['jobdesc'][:1800]}\n"
        )

    prompt = (
        "Anda adalah penyaring lowongan kerja untuk Genta (Fresh Graduate Teknik Elektro, "
        "pengalaman industri 0–1 tahun; proyek: Embedded STM32/Arduino, PCB KiCad, "
        "Power Electronics/Inverter, PLC/Otomasi, Maintenance Elektrikal, Power System, QC Hardware).\n\n"
        "Evaluasi SETIAP lowongan berikut dan kembalikan hasilnya sesuai ID masing-masing:\n\n"
        + "\n---\n".join(blok_loker)
        + "\n\nATURAN KETAT:\n"
        "1. relevan=true HANYA jika menerima Fresh Graduate, 0–1 tahun pengalaman, Junior, Management Trainee, "
        "atau tidak mewajibkan pengalaman >= 2 tahun.\n"
        "2. relevan=false jika mewajibkan pengalaman minimal 2 tahun ke atas, atau bidangnya bukan teknik elektro/"
        "otomasi/maintenance/embedded/power/QC hardware.\n"
        "3. alasan_singkat WAJIB sangat padat (maksimal 12 kata), sebutkan syarat pengalaman & kecocokan utamanya.\n"
    )

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "OBJECT",
                "properties": {
                    "hasil": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "id": {"type": "STRING"},
                                "relevan": {"type": "BOOLEAN"},
                                "posisi": {"type": "STRING"},
                                "perusahaan": {"type": "STRING"},
                                "alasan_singkat": {"type": "STRING"},
                            },
                            "required": ["id", "relevan", "posisi", "perusahaan", "alasan_singkat"],
                        },
                    }
                },
                "required": ["hasil"],
            },
        ),
    )

    data = json.loads(response.text)
    return data.get("hasil", [])


# ================= PROSES UTAMA (JOBSTREET + GLINTS) =================
def cek_loker_semua_portal():
    if not scan_lock.acquire(blocking=False):
        return

    try:
        pastikan_file_feed_ada()

        if not GEMINI_API_KEY:
            msg = "Isi dulu GEMINI_API_KEY di file .env!"
            print(f"[STOP] {msg}")
            with state_lock:
                BOT_STATE["last_result"] = msg
            return

        with state_lock:
            BOT_STATE["is_scanning"] = True
            BOT_STATE["last_result"] = "Sedang memindai Jobstreet & Glints..."

        mulai = time.time()
        jam_now = datetime.now().strftime("%H:%M:%S")
        print(f"[{jam_now}] Memindai Jobstreet & Glints...", end="", flush=True)

        client = genai.Client(api_key=GEMINI_API_KEY)
        riwayat = muat_riwayat()
        riwayat_awal = set(riwayat)
        jumlah_riwayat_awal = len(riwayat)

        session = cf_requests.Session(impersonate="chrome")
        id_sudah_diantre = set()

        # ---------- 1. COLLECT JOBSTREET ----------
        loker_jobstreet = []
        try:
            session.get("https://id.jobstreet.com/id", timeout=15)
            time.sleep(random.uniform(0.4, 0.8))
        except Exception:
            pass

        for kw in KEYWORDS_JOBSTREET:
            try:
                daftar = ambil_loker_jobstreet(session, kw, jumlah=8)
                for job in daftar:
                    job_id = str(job.get("id", "")).strip()
                    judul = job.get("title", "")

                    if not job_id or job_id in riwayat or job_id in id_sudah_diantre:
                        continue

                    if any(kata in judul.lower() for kata in KATA_TERLARANG_JUDUL):
                        riwayat.add(job_id)
                        continue

                    bullets = job.get("bulletPoints") or []
                    teks_fallback = job.get("teaser", "")
                    if bullets:
                        teks_fallback += "\n• " + "\n• ".join(bullets)

                    if jelas_butuh_pengalaman_tinggi(teks_fallback):
                        riwayat.add(job_id)
                        continue

                    company = (
                        job.get("companyName")
                        or (job.get("advertiser") or {}).get("description")
                        or "Tidak disebutkan"
                    )
                    locs = job.get("locations") or []
                    lokasi = locs[0].get("label", "Indonesia") if locs else "Indonesia"

                    loker_jobstreet.append({
                        "id": job_id,
                        "source": "Jobstreet",
                        "title": judul,
                        "company": company,
                        "location": lokasi,
                        "salary": job.get("salaryLabel") or "Tidak dicantumkan",
                        "fallback_desc": teks_fallback.strip(),
                        "listing_date_iso": job.get("listingDate") or "",
                        "link": f"https://id.jobstreet.com/id/job/{job_id}",
                    })
                    id_sudah_diantre.add(job_id)

                time.sleep(random.uniform(0.3, 0.7))
            except Exception as e:
                tulis_log(f"[JOBSTREET ERROR] {kw}: {e}")
                continue

        # ---------- 2. COLLECT GLINTS ----------
        loker_glints = []
        for kw in KEYWORDS_GLINTS:
            try:
                daftar_g = ambil_loker_glints(session, kw, jumlah=8)
                for job in daftar_g:
                    raw_id = str(job.get("id", "")).strip()
                    if not raw_id:
                        continue
                    job_id = f"glints_{raw_id}"
                    judul = (job.get("title") or "").strip()

                    if not judul or job_id in riwayat or job_id in id_sudah_diantre:
                        continue

                    if any(kata in judul.lower() for kata in KATA_TERLARANG_JUDUL):
                        riwayat.add(job_id)
                        continue

                    # Filter cepat angka pengalaman langsung dari metadata Glints
                    min_exp = job.get("minYearsOfExperience")
                    if isinstance(min_exp, (int, float)) and min_exp >= 2:
                        riwayat.add(job_id)
                        continue

                    company = (job.get("company") or {}).get("name") or "Tidak disebutkan"
                    lokasi = ekstrak_lokasi_glints(job)
                    gaji = format_gaji_glints(job.get("salaries"))

                    # Ambil skill tags / ringkasan sebagai fallback
                    skills = job.get("skills") or []
                    skill_names = [
                        s.get("skill", {}).get("name") or s.get("name", "")
                        for s in skills
                        if isinstance(s, dict)
                    ]
                    skill_names = [s for s in skill_names if s]
                    exp_label = (
                        f"Pengalaman minimal: {min_exp} tahun"
                        if isinstance(min_exp, (int, float))
                        else "Syarat pengalaman: lihat detail"
                    )
                    teks_fallback = exp_label
                    if skill_names:
                        teks_fallback += "\nSkill: " + ", ".join(skill_names)

                    slug = buat_slug(judul)
                    link_glints = f"https://glints.com/id/opportunities/jobs/{slug}/{raw_id}"

                    loker_glints.append({
                        "id": job_id,
                        "source": "Glints",
                        "title": judul,
                        "company": company,
                        "location": lokasi,
                        "salary": gaji,
                        "fallback_desc": teks_fallback,
                        "listing_date_iso": job.get("updatedAt") or job.get("createdAt") or "",
                        "link": link_glints,
                    })
                    id_sudah_diantre.add(job_id)

                time.sleep(random.uniform(0.4, 0.8))
            except Exception as e:
                tulis_log(f"[GLINTS ERROR] {kw}: {e}")
                continue

        # Urutkan masing-masing dari terbaru, lalu gabung selang-seling agar seimbang
        loker_jobstreet.sort(key=lambda x: x["listing_date_iso"], reverse=True)
        loker_glints.sort(key=lambda x: x["listing_date_iso"], reverse=True)

        loker_gabungan = []
        max_len = max(len(loker_jobstreet), len(loker_glints)) if (loker_jobstreet or loker_glints) else 0
        for i in range(max_len):
            if i < len(loker_jobstreet):
                loker_gabungan.append(loker_jobstreet[i])
            if i < len(loker_glints):
                loker_gabungan.append(loker_glints[i])

        # ---------- 3. TARIK DETAIL JOBDESC ----------
        kandidat_siap_ai = []
        for loker in loker_gabungan[:MAX_LOKER_PER_RUN]:
            if loker["source"] == "Glints":
                jobdesc_lengkap = ambil_detail_glints(session, loker["link"], loker["fallback_desc"])
            else:
                jobdesc_lengkap = ambil_detail_jobstreet(session, loker["id"], loker["fallback_desc"])

            if jelas_butuh_pengalaman_tinggi(jobdesc_lengkap):
                riwayat.add(loker["id"])
                continue

            loker["jobdesc"] = jobdesc_lengkap
            kandidat_siap_ai.append(loker)
            time.sleep(random.uniform(0.3, 0.6))

        # ---------- 4. BATCH AI SCREENING ----------
        loker_lolos = []
        if kandidat_siap_ai:
            try:
                hasil_ai = screening_batch_ai(client, kandidat_siap_ai)
                peta_loker = {k["id"]: k for k in kandidat_siap_ai}

                for item in hasil_ai:
                    jid = str(item.get("id", "")).strip()
                    if jid not in peta_loker:
                        continue
                    riwayat.add(jid)

                    if item.get("relevan"):
                        src = peta_loker[jid]
                        loker_lolos.append({
                            "id": jid,
                            "source": src["source"],
                            "posisi": item.get("posisi") or src["title"],
                            "perusahaan": item.get("perusahaan") or src["company"],
                            "location": src["location"],
                            "salary": src["salary"],
                            "alasan_singkat": item.get("alasan_singkat") or "",
                            "jobdesc": src["jobdesc"],
                            "link": src["link"],
                        })

                for k in kandidat_siap_ai:
                    riwayat.add(k["id"])

            except Exception as e:
                tulis_log(f"[ERROR AI] {e}")

        if len(riwayat) > jumlah_riwayat_awal:
            simpan_riwayat(riwayat, id_baru=riwayat - riwayat_awal)

        durasi = time.time() - mulai
        jam_selesai = datetime.now().strftime("%H:%M WIB")

        if loker_lolos:
            lolos_jobstreet = [x for x in loker_lolos if x.get("source") != "Glints"]
            lolos_glints    = [x for x in loker_lolos if x.get("source") == "Glints"]

            if lolos_jobstreet:
                simpan_ke_feed_dashboard(lolos_jobstreet, source="Jobstreet")

            if lolos_glints:
                simpan_ke_feed_dashboard(lolos_glints, source="Glints")

            status_kirim = "APK Karir & Cloud OK"

            ringkasan = f"Jobstreet: {len(lolos_jobstreet)} • Glints: {len(lolos_glints)} ({durasi:.1f}d)"

            print(f" Selesai ({durasi:.1f}d) -> {len(loker_lolos)} loker cocok [{status_kirim}]:")
            for idx, lk in enumerate(loker_lolos, 1):
                print(f"  {idx}. [{lk['source']}] {lk['posisi']} | {lk['perusahaan']} ({lk['location']}) | {lk['salary']}")
                print(f"     Catatan: {lk['alasan_singkat']}")
        else:
            ringkasan = f"Belum ada loker baru yang cocok ({durasi:.1f}d)"
            print(f" Selesai ({durasi:.1f}d) -> Belum ada loker baru yang cocok.")

        with state_lock:
            BOT_STATE["last_scan_at"] = jam_selesai
            BOT_STATE["last_result"] = ringkasan

    finally:
        with state_lock:
            BOT_STATE["is_scanning"] = False
        scan_lock.release()


# ================= WORKER LOOP OTOMATIS =================
def worker_loop():
    while True:
        with state_lock:
            mode = BOT_STATE["mode"]
            auto_run = BOT_STATE["auto_run"]
            force_scan = BOT_STATE["force_scan"]
            if force_scan:
                BOT_STATE["force_scan"] = False

        if force_scan:
            cek_loker_semua_portal()
            continue

        if mode == "laptop_nyala" and auto_run:
            cek_loker_semua_portal()
            jeda = random.randint(600, 900)
            berikutnya = (datetime.now() + timedelta(seconds=jeda)).strftime("%H:%M WIB")
            with state_lock:
                BOT_STATE["next_run_at"] = berikutnya
            wake_event.clear()
            wake_event.wait(timeout=jeda)
        else:
            with state_lock:
                BOT_STATE["next_run_at"] = "-"
            wake_event.clear()
            wake_event.wait(timeout=5)


# ================= SERVER LOKAL + API SAKLAR =================
class DashboardHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        nama_file = os.path.basename(path).lower()
        if any(part.startswith(".") for part in path.split("/")) or nama_file.endswith((".py", ".log", ".vbs", ".bat", ".env")):
            return self._send_json({"error": "Akses ditolak"}, status=403)

        if path == "/api/status":
            with state_lock:
                data = dict(BOT_STATE)
            data["cloud_ready"] = cloud_terkonfigurasi()
            return self._send_json(data)

        if path in ("/", f"/{HTML_FILE}"):
            target = os.path.join(BASE_DIR, HTML_FILE)
            if os.path.exists(target):
                with open(target, "r", encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:
            payload = {}

        if path == "/api/toggle":
            with state_lock:
                if "auto_run" in payload:
                    BOT_STATE["auto_run"] = bool(payload["auto_run"])
                else:
                    BOT_STATE["auto_run"] = not BOT_STATE["auto_run"]
                if BOT_STATE["auto_run"]:
                    BOT_STATE["mode"] = "laptop_nyala"
                    BOT_STATE["last_result"] = "Auto-Run dinyalakan."
                else:
                    BOT_STATE["last_result"] = "Auto-Run dijeda (OFF)."
                status_now = dict(BOT_STATE)
            wake_event.set()
            status_now["cloud_ready"] = cloud_terkonfigurasi()
            return self._send_json(status_now)

        if path == "/api/mode":
            target_mode = payload.get("mode", "laptop_nyala")
            if target_mode == "laptop_mati":
                with state_lock:
                    BOT_STATE["mode"] = "laptop_mati"
                    BOT_STATE["auto_run"] = False
                    BOT_STATE["next_run_at"] = "-"
                wake_event.set()
                _, pesan_cloud = atur_github_workflow(True)
                with state_lock:
                    BOT_STATE["cloud_status"] = pesan_cloud
                    BOT_STATE["last_result"] = "Mode Laptop Mati aktif (bot laptop dijeda)."
                    status_now = dict(BOT_STATE)
            else:
                _, pesan_cloud = atur_github_workflow(False)
                with state_lock:
                    BOT_STATE["mode"] = "laptop_nyala"
                    BOT_STATE["auto_run"] = True
                    BOT_STATE["cloud_status"] = pesan_cloud if cloud_terkonfigurasi() else ""
                    BOT_STATE["last_result"] = "Mode Laptop Nyala aktif (Auto-Run ON)."
                    status_now = dict(BOT_STATE)
                wake_event.set()

            status_now["cloud_ready"] = cloud_terkonfigurasi()
            return self._send_json(status_now)

        if path == "/api/scan-now":
            with state_lock:
                if not BOT_STATE["is_scanning"]:
                    BOT_STATE["force_scan"] = True
                    wake_event.set()
            return self._send_json({"ok": True, "message": "Pemindaian langsung dimulai."})

        return self._send_json({"error": "Endpoint tidak ditemukan"}, status=404)


if __name__ == "__main__":
    if "--cloud" in sys.argv:
        cek_loker_semua_portal()
        sys.exit(0)

    pastikan_file_feed_ada()

    handler = functools.partial(DashboardHandler, directory=BASE_DIR)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    except OSError:
        print("Radar Loker sudah aktif di background. Tidak perlu dijalankan dua kali.")
        sys.exit(0)

    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()

    url_dashboard = f"http://localhost:{PORT}/{HTML_FILE}"
    print("============================================================")
    print("  RADAR LOKER AKTIF: JOBSTREET + GLINTS (AUTO-LOOP ON)")
    print(f"  Dashboard : {url_dashboard}")
    print("  Jadwal    : Tiap 10-15 menit (07:30 - 19:00 WIB)")
    print("  Tekan Ctrl + C kapan saja untuk mematikan.")
    print("============================================================\n")

    if "--buka" in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url_dashboard)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nRadar Loker dimatikan.")
        server.server_close()
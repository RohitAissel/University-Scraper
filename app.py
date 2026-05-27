import io
import json
import re
import subprocess
from urllib.parse import urljoin, urlparse
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from lattes_playwright import scrape_lattes_all_records

# PLAYWRIGHT SUPPORT

try:
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


# PAGE INITIATION LOGIC


def initialize_session_state():
    default_states = {
        "scrape_log": [],
        "combined_records": [],
        "analysis_complete": False,
        "error_message": "",
        "download_json": "",
        "download_excel": b"",
        "processed_urls": [],
        "total_pages_crawled": 0,
        "url_progress": {},
    }

    for key, value in default_states.items():
        if key not in st.session_state:
            st.session_state[key] = value


initialize_session_state()


# PAGE CONFIGURATION


st.set_page_config(
    page_title="DeepSeek University Directory Scraper",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)


# CUSTOM PAGE STYLING


st.markdown(
    """
    <style>
        .main {
            padding-top: 1rem;
        }

        .stButton > button {
            width: 100%;
            height: 3em;
            font-size: 18px;
            font-weight: 600;
        }

        .metric-box {
            background-color: #f3f4f6;
            padding: 1rem;
            border-radius: 10px;
            text-align: center;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


# HEADER SECTION


st.title("🤖 DeepSeek University Directory Scraper")

st.markdown("""
AI-powered university leadership extraction system.

This tool:
- Scrapes a list of university leadership pages
- Handles common pagination patterns like numbered pages and next-page links
- Tracks progress per URL in real time
- Exports a single combined JSON or Excel file with the source URL for every record
""")


# SIDEBAR CONFIGURATION


with st.sidebar:
    st.header("🔑 API Configuration")

    api_key_input = st.text_input(
        "DeepSeek API Key",
        type="password",
        help="Enter your DeepSeek API key.",
    )

    st.header("🌐 University URLs")

    university_urls_input = st.text_area(
        "Paste one URL per line",
        value=("https://www.ubc.ca\n" "https://www.mcgill.ca"),
        height=180,
    )

    st.header("⚙️ Scraping Options")

    use_playwright = st.checkbox(
        "Enable JavaScript Rendering (Playwright)",
        value=False,
        help="Use this for pages that require JavaScript rendering.",
    )

    max_pages_per_university = st.number_input(
        "Maximum pages per university",
        min_value=1,
        max_value=20,
        value=5,
    )

    if use_playwright and not PLAYWRIGHT_AVAILABLE:
        st.warning(
            "Playwright is not installed.\n\n"
            "Run:\n"
            "pip install playwright\n"
            "playwright install"
        )

    st.header("🔍 AI Filtering Instructions")

    user_prompt_instruction = st.text_area(
        "Target Persona Filtering Guideline",
        value=(
            "Extract executive leadership, deans, vice-presidents, "
            "associate vice-presidents, directors, department heads, "
            "senior academic administrators, provosts, chancellors, "
            "and institutional leadership personnel."
        ),
        height=200,
    )


# PYDANTIC MODELS


class LeaderProfile(BaseModel):
    name: str = Field(description="Full name of the academic leader.")
    title: str = Field(description="Professional title.")
    email: str = Field(description="Professional email address.")
    bio_url: str = Field(description="Biography/profile URL.")


class LeadershipDirectory(BaseModel):
    profiles: list[LeaderProfile]


# HELPERS


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml," "application/xml;q=0.9,image/webp,*/*;q=0.8"
    ),
    "Connection": "keep-alive",
}


def make_empty_url_progress():
    return {
        "status": "queued",
        "messages": [],
        "records": [],
        "pages_visited": 0,
        "error": "",
    }


def save_url_progress(
    url: str,
    *,
    status: str | None = None,
    message: str | None = None,
    records: list[dict] | None = None,
    pages_visited: int | None = None,
    error: str | None = None,
):
    progress = st.session_state.url_progress.setdefault(url, make_empty_url_progress())

    if status is not None:
        progress["status"] = status

    if message:
        progress["messages"].append(message)

    if records is not None:
        progress["records"] = records

    if pages_visited is not None:
        progress["pages_visited"] = pages_visited

    if error is not None:
        progress["error"] = error


def render_url_progress():
    if not st.session_state.url_progress:
        st.info("No URL progress has been saved yet.")
        return

    for url, progress in st.session_state.url_progress.items():
        with st.expander(f"{url} — {progress['status']}", expanded=False):
            st.write(f"Status: **{progress['status']}**")
            st.write(f"Pages visited: {progress['pages_visited']}")
            st.write(f"Saved records: {len(progress['records'])}")

            if progress["error"]:
                st.error(progress["error"])

            if progress["messages"]:
                for message in progress["messages"]:
                    st.write(message)
            else:
                st.write("No messages logged yet.")


def normalize_source_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        return ""
    if cleaned.startswith("www."):
        cleaned = f"https://{cleaned}"
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    return cleaned


def parse_university_urls(raw_text: str) -> list[str]:
    urls = []
    for line in raw_text.splitlines():
        for chunk in re.split(r"\s*,\s*", line):
            candidate = chunk.strip()
            if not candidate:
                continue
            normalized = normalize_source_url(candidate)
            if normalized:
                urls.append(normalized)
    return urls


def fetch_html_with_curl(url: str) -> str:
    try:
        result = subprocess.run(
            [
                "curl",
                "-L",
                "--max-time",
                "30",
                "--retry",
                "2",
                "--retry-delay",
                "1",
                "--compressed",
                "--user-agent",
                REQUEST_HEADERS["User-Agent"],
                url,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("curl is not installed in this environment.") from exc

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"curl failed with exit code {result.returncode}: {stderr}")

    return result.stdout


def fetch_html(url: str, enable_playwright: bool = False) -> str:
    if enable_playwright and PLAYWRIGHT_AVAILABLE:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(1500)
                html = page.content()
                browser.close()
            return html
        except Exception:
            pass

    try:
        return fetch_html_with_curl(url)
    except Exception:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
        response.raise_for_status()
        return response.text


def clean_html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for element in soup(
        [
            "script",
            "style",
            "nav",
            "footer",
            "header",
            "noscript",
            "svg",
            "img",
            "iframe",
            "form",
            "button",
        ]
    ):
        element.decompose()

    text = soup.get_text(separator="\n", strip=True)

    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped not in cleaned_lines:
            cleaned_lines.append(stripped)

    return "\n".join(cleaned_lines)


def extract_profiles_with_deepseek(api_key: str, text_content: str, instructions: str):
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert university directory extraction AI.\n\n"
                    "Extract leadership personnel from webpage text.\n\n"
                    "Return ONLY raw JSON.\n\n"
                    "Required schema:\n\n"
                    "{\n"
                    '  "profiles": [\n'
                    "    {\n"
                    '      "name": "",\n'
                    '      "title": "",\n'
                    '      "email": "",\n'
                    '      "bio_url": ""\n'
                    "    }\n"
                    "  ]\n"
                    "}\n\n"
                    "Rules:\n"
                    "- Preserve exact names\n"
                    "- Combine fragmented titles\n"
                    "- Return blank strings if unavailable\n"
                    "- Ignore navigation text\n"
                    "- Ignore unrelated staff\n"
                ),
            },
            {
                "role": "user",
                "content": f"Extraction Instructions:\n{instructions}\n\nWebpage Content:\n{text_content}",
            },
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    response = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )

    response.raise_for_status()
    data = response.json()
    raw_json = data["choices"][0]["message"]["content"]
    return json.loads(raw_json)


def normalize_profile(raw_profile: dict, source_url: str) -> dict:
    return {
        "name": str(raw_profile.get("name", "") or "").strip(),
        "title": str(raw_profile.get("title", "") or "").strip(),
        "email": str(raw_profile.get("email", "") or "").strip(),
        "bio_url": str(raw_profile.get("bio_url", "") or "").strip(),
        "source_url": source_url,
    }


def extract_page_records(
    url: str,
    api_key: str,
    instructions: str,
    enable_playwright: bool,
) -> tuple[list[dict], str]:
    html = fetch_html(url, enable_playwright=enable_playwright)
    text = clean_html_to_text(html)

    if len(text) > 45000:
        text = text[:45000]

    extracted = extract_profiles_with_deepseek(api_key, text, instructions)
    profiles = extracted.get("profiles", [])

    records = [normalize_profile(profile, url) for profile in profiles]
    return records, html


def same_domain(url_a: str, url_b: str) -> bool:
    host_a = urlparse(url_a).netloc.lower()
    host_b = urlparse(url_b).netloc.lower()

    if not host_a or not host_b:
        return False

    return (
        host_a == host_b
        or host_a.endswith(f".{host_b}")
        or host_b.endswith(f".{host_a}")
    )


def extract_page_number(url: str) -> int | None:
    parsed = urlparse(url)

    candidates = []
    for value in [parsed.query, parsed.path]:
        matches = re.findall(r"(?:^|[\D])(\d+)(?:$|[\D])", value)
        if matches:
            candidates.extend(int(match) for match in matches)

    if not candidates:
        return None

    return max(candidates)


def clean_text(text: str) -> str:
    return " ".join(text.split())


def normalize_link(current_url: str, href: str) -> str:
    href = href.strip()
    if not href or href.startswith(("javascript:", "mailto:", "#")):
        return ""

    normalized = urljoin(current_url, href)
    return normalized


def score_pagination_candidate(current_url: str, href: str, text: str) -> int:
    text_lower = text.lower()
    href_lower = href.lower()
    score = 0

    next_keywords = (
        "next",
        "older",
        "newer",
        "load more",
        "load-more",
        "show more",
        "more results",
        "continue",
        "more",
    )

    if any(keyword in text_lower for keyword in next_keywords):
        score += 5

    if "page" in text_lower and re.search(r"\d+", text_lower):
        score += 4

    if re.fullmatch(r"\d+", text_lower):
        score += 3

    if any(
        keyword in href_lower
        for keyword in ("page", "offset", "start", "p=", "pg=", "per_page")
    ):
        score += 2

    if "next" in href_lower or "loadmore" in href_lower or "load-more" in href_lower:
        score += 4

    current_page = extract_page_number(current_url)
    candidate_page = extract_page_number(href)

    if current_page is not None and candidate_page is not None:
        if candidate_page > current_page:
            score += 5
        elif candidate_page < current_page:
            score -= 2

    if href == current_url:
        score -= 100

    return score


def discover_next_urls(current_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    for tag in soup.find_all(["a", "button"], href=True):
        href = tag.get("href", "")
        normalized = normalize_link(current_url, href)
        if not normalized:
            continue
        if not same_domain(current_url, normalized):
            continue
        text = clean_text(tag.get_text(" ", strip=True))
        if text:
            candidates.append((normalized, text))

    for tag in soup.find_all(attrs={"data-href": True}):
        href = tag.get("data-href", "")
        normalized = normalize_link(current_url, href)
        if not normalized:
            continue
        if not same_domain(current_url, normalized):
            continue
        text = clean_text(tag.get_text(" ", strip=True) or tag.get("aria-label", ""))
        if text:
            candidates.append((normalized, text))

    for tag in soup.find_all(attrs={"data-url": True}):
        href = tag.get("data-url", "")
        normalized = normalize_link(current_url, href)
        if not normalized:
            continue
        if not same_domain(current_url, normalized):
            continue
        text = clean_text(tag.get_text(" ", strip=True) or tag.get("aria-label", ""))
        if text:
            candidates.append((normalized, text))

    scored = []
    for href, text in candidates:
        score = score_pagination_candidate(current_url, href, text)
        if score > 0:
            scored.append((score, href, text))

    scored.sort(key=lambda item: item[0], reverse=True)

    seen = []
    for _, href, _ in scored:
        if href not in seen:
            seen.append(href)

    return seen


def build_export_payload(records: list[dict]) -> tuple[pd.DataFrame, str, bytes]:
    if not records:
        empty_df = pd.DataFrame(
            columns=["name", "title", "email", "bio_url", "source_url"]
        )
        return empty_df, "[]", b""

    df = pd.DataFrame(records)

    ordered_columns = ["name", "title", "email", "bio_url", "source_url"]
    for column in ordered_columns:
        if column not in df.columns:
            df[column] = ""

    df = df[ordered_columns]
    json_payload = json.dumps(records, indent=2, ensure_ascii=False)

    buffer = io.BytesIO()
    df.to_excel(buffer, index=False, engine="openpyxl")
    excel_bytes = buffer.getvalue()

    return df, json_payload, excel_bytes


def crawl_university(
    seed_url: str,
    api_key: str,
    instructions: str,
    enable_playwright: bool,
    max_pages_per_university: int,
    on_progress,
):
    if seed_url.startswith("https://buscatextual.cnpq.br/buscatextual/busca.do"):
        on_progress("Using CNPq Playwright workflow for this URL.")
        records, pages_visited = scrape_lattes_all_records(
            max_pages=max_pages_per_university,
            source_url=seed_url,
        )
        return records, pages_visited

    queue = [seed_url]
    visited = set()
    all_records = []
    pages_visited = 0

    while queue and pages_visited < max_pages_per_university:
        current_url = queue.pop(0)

        if current_url in visited:
            continue

        visited.add(current_url)
        pages_visited += 1

        on_progress(f"Fetching page {pages_visited}: {current_url}")
        records, html = extract_page_records(
            current_url,
            api_key,
            instructions,
            enable_playwright,
        )
        all_records.extend(records)

        next_urls = discover_next_urls(current_url, html)
        for candidate in next_urls:
            if candidate not in visited and candidate not in queue:
                queue.append(candidate)

        if next_urls:
            on_progress(
                f"Found {len(next_urls)} pagination candidate(s) on {current_url}."
            )
        else:
            on_progress(f"No pagination candidates found on {current_url}.")

    return all_records, pages_visited


# MAIN ACTION BUTTON


analyze_clicked = st.button("🚀 Scrape All Universities")

if analyze_clicked:
    st.session_state.analysis_complete = False
    st.session_state.error_message = ""
    st.session_state.scrape_log = []
    st.session_state.combined_records = []
    st.session_state.processed_urls = []
    st.session_state.download_json = ""
    st.session_state.download_excel = b""
    st.session_state.total_pages_crawled = 0
    st.session_state.url_progress = {}

    urls = parse_university_urls(university_urls_input)

    if not urls:
        st.error("Please enter at least one valid university URL.")
        st.stop()

    requires_deepseek = any(
        not url.startswith("https://buscatextual.cnpq.br/buscatextual/busca.do")
        for url in urls
    )

    if requires_deepseek and not api_key_input:
        st.error("Please enter your DeepSeek API key.")
        st.stop()

    total_urls = len(urls)
    progress_bar = st.progress(0, text="Preparing scrape")
    current_url = None

    try:
        with st.status("Starting multi-university scrape", expanded=True) as status:
            for index, url in enumerate(urls, start=1):
                current_url = url
                save_url_progress(
                    url,
                    status="running",
                    message=f"[{index}/{total_urls}] Starting: {url}",
                )
                status.update(
                    label=f"Processing {index}/{total_urls}: {url}",
                    state="running",
                )
                status.write("Starting crawl for this URL.")

                def log_message(message: str):
                    save_url_progress(url, message=f"[{index}/{total_urls}] {message}")
                    status.write(message)

                records, pages_visited = crawl_university(
                    seed_url=url,
                    api_key=api_key_input,
                    instructions=user_prompt_instruction,
                    enable_playwright=use_playwright,
                    max_pages_per_university=max_pages_per_university,
                    on_progress=log_message,
                )

                save_url_progress(
                    url,
                    status="completed",
                    records=records,
                    pages_visited=pages_visited,
                    message=f"[{index}/{total_urls}] Completed {url}: {len(records)} records across {pages_visited} page(s).",
                )

                st.session_state.combined_records.extend(records)
                st.session_state.processed_urls.append(url)
                st.session_state.total_pages_crawled += pages_visited
                st.session_state.scrape_log.append(
                    f"[{index}/{total_urls}] Completed {url}: {len(records)} records across {pages_visited} page(s)."
                )
                status.write(
                    f"Completed {url}: {len(records)} records across {pages_visited} page(s)."
                )
                progress_bar.progress(index / total_urls)

        # Final dedupe across all collected records (by `id` then `bio_url`) to ensure UI/export consistency
        unique_records = []
        seen_ids = set()
        for rec in st.session_state.combined_records:
            rid = rec.get("id") or rec.get("bio_url") or (rec.get("name", "") + rec.get("title", ""))
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            unique_records.append(rec)

        st.session_state.combined_records = unique_records

        df, json_payload, excel_bytes = build_export_payload(
            st.session_state.combined_records
        )
        st.session_state.download_json = json_payload
        st.session_state.download_excel = excel_bytes
        st.session_state.analysis_complete = True

    except Exception as e:
        st.session_state.error_message = str(e)
        if current_url:
            save_url_progress(current_url, status="error", error=str(e))


# RESULTS SECTION


if st.session_state.error_message:
    st.error(st.session_state.error_message)

if st.session_state.analysis_complete:
    df = pd.DataFrame(st.session_state.combined_records)

    st.subheader("📊 Scrape Summary")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Universities Processed", len(st.session_state.processed_urls))
    with col2:
        st.metric("Pages Crawled", st.session_state.total_pages_crawled)
    with col3:
        st.metric("Records Extracted", len(df))

    st.subheader("📈 Progress Tracker")
    render_url_progress()

    st.subheader("📋 Combined Directory")
    st.dataframe(df, use_container_width=True, height=600)

    download_col1, download_col2 = st.columns(2)

    with download_col1:
        st.download_button(
            label="📥 Download JSON",
            data=st.session_state.download_json.encode("utf-8"),
            file_name="university_leadership_directory.json",
            mime="application/json",
        )

    with download_col2:
        st.download_button(
            label="📥 Download Excel",
            data=st.session_state.download_excel,
            file_name="university_leadership_directory.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

elif st.session_state.url_progress:
    st.subheader("📈 Progress Tracker")
    render_url_progress()
elif st.session_state.scrape_log:
    st.subheader("📈 Progress Tracker")
    for entry in st.session_state.scrape_log:
        st.write(entry)

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import time
import re


def get_stable_page_content(page, retries=5, wait_seconds=1.5):
    last_exc = None
    for attempt in range(retries):
        try:
            return page.content()
        except Exception as exc:
            last_exc = exc
            if attempt == retries - 1:
                raise
            page.wait_for_timeout(wait_seconds * 1000)
    raise last_exc


def extract_cnpq_records(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for li in soup.select("li"):
        anchor = li.find("a", href=True)
        if not anchor:
            continue

        href = anchor.get("href", "")
        if "abreDetalhe" not in href:
            continue

        name = anchor.get_text(strip=True)
        text = li.get_text(" ", strip=True)
        if not name or text == "Stale file handle":
            continue

        title = text.replace(name, "", 1).strip()
        # extract a stable id from the JavaScript detail link if possible
        rec_id = None
        m = re.search(r"abreDetalhe\('\s*([^'\s]+)\s*'", href)
        if m:
            rec_id = m.group(1)

        records.append(
            {
                "id": rec_id or href,
                "name": name,
                "title": title,
                "email": "",
                "bio_url": href,
            }
        )

    return records


def scrape_lattes_all_records(max_pages=1000, delay=1.0, source_url=None):
    """Automate the CNPq Lattes search workflow and extract paginated results."""
    url = source_url or "https://buscatextual.cnpq.br/buscatextual/busca.do"
    results = []
    pages_scraped = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Robust navigation: retry navigation a few times to handle transient DNS/network errors
        nav_retries = 3
        nav_ok = False
        for attempt in range(nav_retries):
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
                nav_ok = True
                break
            except Exception as exc:
                print(f"[Playwright] Page.goto attempt {attempt+1} failed: {exc}")
                # backoff
                time.sleep(2 ** attempt)

        if not nav_ok:
            print("[Playwright] Failed to navigate after retries; aborting Playwright flow.")
            try:
                browser.close()
            except Exception:
                pass
            return [], 0

        time.sleep(delay)

        page.click("text=Atividade Profissional (Instituição)")
        time.sleep(delay)

        visibility_info = page.locator("a#preencheCategoriaNivelBolsa").evaluate_all(
            "els => els.map((el, idx) => ({idx, rect: el.getBoundingClientRect()}))"
        )

        visible_idx = None
        for item in visibility_info:
            rect = item.get("rect") or {}
            if rect.get("width", 0) > 0 and rect.get("height", 0) > 0:
                visible_idx = item["idx"]
                break

        if visible_idx is None:
            browser.close()
            return [], 0

        aplicar_el = page.locator("a#preencheCategoriaNivelBolsa").nth(visible_idx)
        if aplicar_el.count() == 0:
            browser.close()
            return [], 0

        aplicar_el.evaluate("el => el.click()")
        page.wait_for_timeout(3000)

        page.click("a:has-text('Buscar')")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)

        seen_ids = set()
        for page_num in range(1, max_pages + 1):
            html = get_stable_page_content(page)
            records = extract_cnpq_records(html)
            if not records:
                print(f"[Playwright] Page {page_num} returned no records; stopping.")
                break

            # add only new records (deduplicate across pages by record id)
            new_count = 0
            for record in records:
                rec_id = record.get("id")
                if rec_id in seen_ids:
                    continue
                seen_ids.add(rec_id)
                record["source_url"] = url
                results.append(record)
                new_count += 1

            print(f"[Playwright] Page {page_num}: html_len={len(html)} records_found={len(records)} new_added={new_count}")

            pages_scraped = page_num

            # Robust pagination: compute next offset and call the page's submeterPaginacao function
            try:
                onclicks = page.eval_on_selector_all(
                    "a[onclick*='submeterPaginacao']",
                    "els => els.map(el => el.getAttribute('onclick') || '')",
                )
            except Exception:
                onclicks = []

            onclick_str = ""
            for s in onclicks:
                if s and "submeterPaginacao" in s:
                    onclick_str = s
                    break

            if not onclick_str:
                break

            m = re.search(r"submeterPaginacao\((\d+),\s*(\d+)\)", onclick_str)
            if m:
                page_size = int(m.group(2))
            else:
                page_size = 10

            next_offset = page_num * page_size

            # If we didn't add any new records on this page, try re-invoking pagination a couple times
            if new_count == 0:
                retries = 2
                retry_ok = False
                for attempt in range(retries):
                    try:
                        print(f"[Playwright] No new records on page {page_num}; retrying pagination (attempt {attempt+1})")
                        page.evaluate(f"() => submeterPaginacao({next_offset}, {page_size})")
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(1500)

                        html = get_stable_page_content(page)
                        records = extract_cnpq_records(html)

                        # add any newly found records
                        for record in records:
                            rec_id = record.get("id")
                            if rec_id in seen_ids:
                                continue
                            seen_ids.add(rec_id)
                            record["source_url"] = url
                            results.append(record)
                            retry_ok = True

                        if retry_ok:
                            break
                    except Exception as exc:
                        print(f"[Playwright] Retry pagination attempt failed: {exc}")
                        page.wait_for_timeout(1000)

                if not retry_ok and new_count == 0:
                    print(f"[Playwright] No new records after retries; stopping at page {page_num}.")
                    break

            try:
                page.evaluate(f"() => submeterPaginacao({next_offset}, {page_size})")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1500)
            except Exception as exc:
                print(f"[Playwright] Pagination invoke failed: {exc}")
                break

        browser.close()

    return results, pages_scraped

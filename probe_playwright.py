from playwright.sync_api import sync_playwright
url='https://buscatextual.cnpq.br/buscatextual/busca.do'
with sync_playwright() as p:
    b=p.chromium.launch(headless=True)
    page=b.new_page()
    try:
        page.goto(url, wait_until='networkidle', timeout=60000)
        page.click("text=Atividade Profissional (Instituição)")
        page.wait_for_timeout(1500)
        onclicks = page.eval_on_selector_all("a[onclick*='submeterPaginacao']", "els => els.map(el => el.getAttribute('onclick') || '')")
        print('onclicks', onclicks)
        print('li count', page.locator('li').count())
    except Exception as e:
        print('probe error', e)
    finally:
        b.close()

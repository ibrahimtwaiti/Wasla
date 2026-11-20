"""Optional Playwright UI steps against the protected staging deployment."""

from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit


@contextmanager
def browser_session(viewport, access_url):
    if not viewport:
        yield None
        return
    from playwright.sync_api import sync_playwright

    if viewport not in {'desktop', 'mobile'}:
        raise ValueError('Use desktop or mobile for STAGING_BROWSER')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        options = (
            playwright.devices['iPhone 13'].copy()
            if viewport == 'mobile'
            else {'viewport': {'width': 1440, 'height': 1000}}
        )
        options.pop('default_browser_type', None)
        context = browser.new_context(**options)
        page = context.new_page()
        page.set_default_timeout(40000)
        try:
            page.goto(access_url, wait_until='domcontentloaded')
            yield page
        finally:
            context.close()
            browser.close()


def sign_up(page, origin, data):
    from playwright.sync_api import expect

    page.goto(origin + '/signup?lang=en')
    for key in ('company', 'name', 'email', 'password'):
        page.locator('[name=' + key + ']').fill(data[key])
    page.locator('[name=plan_id]').select_option(data['plan_id'])
    page.locator('[name=terms]').check()
    page.locator('#signup-form button[type=submit]').click()
    expect(page.locator('#signup-status')).to_contain_text('verification email', timeout=40000)


def activate_and_login(page, origin, verification, address, password):
    from playwright.sync_api import expect

    page.goto(verification)
    page.locator('#verify-form button').click()
    expect(page.locator('#verify-status')).to_contain_text('Email verified', timeout=40000)
    if urlsplit(page.url).fragment:
        raise RuntimeError('Verification token remained in address bar')
    page.goto(origin + '/login?lang=en')
    page.locator('[name=email]').fill(address)
    page.locator('[name=password]').fill(password)
    page.locator('button[type=submit]').click()
    page.wait_for_url('**/app')
    page.goto(origin + '/app/billing?lang=en')
    expect(page.locator('#main')).to_contain_text('Staging verification', timeout=40000)
    page.goto(origin + '/app/channels?lang=en')
    expect(page.locator('#meta-prepare')).to_be_disabled(timeout=40000)


def delete_workspace(page, origin, password):
    from playwright.sync_api import expect

    page.goto(origin + '/app/privacy?lang=ar')
    expect(page.locator('html')).to_have_attribute('dir', 'rtl')
    if not page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'):
        raise RuntimeError('RTL viewport overflow')
    page.locator('[name=scope]').select_option('tenant')
    page.locator('[name=password]').fill(password)
    page.locator('[name=confirm]').fill('DELETE')
    page.locator('#deletion-form button').click()
    page.wait_for_url('**/deletion-status?request=*')
    return parse_qs(urlsplit(page.url).query)['request'][0]

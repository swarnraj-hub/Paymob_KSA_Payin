#!/usr/bin/env python3
"""
PayMob KSA Portal2 - Transactions Report Download + S3 Upload

Flow:
  1. Login to ksa.paymob.com/portal2 (phone + password)
  2. Navigate to Reports & Statements
  3. Select Report Type: Transactions
  4. Set date range (default: last 10 days)
  5. Click Generate and download the report
  6. Upload to s3://payout-recon/paymob/ksa/Weekly/raw/

Usage:
    python paymob_ksa_transactions.py
    python paymob_ksa_transactions.py --start_date 2026-05-11 --end_date 2026-05-21
"""

import argparse
import asyncio
import os
import sys
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from datetime import datetime, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser()
_parser.add_argument("--start_date", type=str, default=None)
_parser.add_argument("--end_date",   type=str, default=None)
_args = _parser.parse_args()

today      = datetime.now()
END_DATE   = _args.end_date   or today.strftime("%Y-%m-%d")
START_DATE = _args.start_date or (today - timedelta(days=10)).strftime("%Y-%m-%d")

START_DT = datetime.strptime(START_DATE, "%Y-%m-%d")
END_DT   = datetime.strptime(END_DATE,   "%Y-%m-%d")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
USERNAME = os.environ.get("PAYMOB_KSA_USERNAME", "")
PASSWORD = os.environ.get("PAYMOB_KSA_PASSWORD", "")

S3_BUCKET  = os.environ.get("S3_BUCKET", "payout-recon")
S3_PREFIX  = os.environ.get("S3_PAYMOB_KSA_PREFIX", "paymob/ksa/Weekly/raw/")
S3_REGION  = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
S3_ENABLED = os.environ.get("PAYMOB_KSA_S3_ENABLED", "true").lower() == "true"

LOGIN_URL    = "https://ksa.paymob.com/portal2/en/login"
DOWNLOAD_DIR = Path("downloads")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def ss(page, name: str) -> None:
    """Take a debug screenshot."""
    path = f"paymob_ksa_{name}.png"
    try:
        await page.screenshot(path=path, full_page=False)
        print(f"  [screenshot] {path}")
    except Exception:
        pass


async def retry_action(fn, retries: int = 3, delay: float = 3.0, label: str = ""):
    """Retry an async callable up to `retries` times with `delay` seconds between attempts."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                print(f"  [retry] {label} attempt {attempt}/{retries} failed: {exc}. "
                      f"Retrying in {delay}s ...")
                await asyncio.sleep(delay)
            else:
                print(f"  [retry] {label} all {retries} attempts failed.")
    raise last_exc


async def wait_for_page_ready(page, selector: str, timeout: int = 20_000) -> bool:
    """Wait for a CSS selector to be visible — confirms the page has fully loaded."""
    try:
        await page.wait_for_selector(selector, state="visible", timeout=timeout)
        return True
    except PwTimeout:
        return False


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
async def do_login(page) -> None:
    print("[login] Navigating to login page ...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)

    # Wait for the login form to be ready (phone field visible)
    if not await wait_for_page_ready(page, 'input[type="tel"], input[placeholder*="Phone"], [aria-label*="Phone"]', 15_000):
        # Fallback: just wait a bit if selector is unusual
        await page.wait_for_timeout(3_000)
    await ss(page, "00_login_page")

    print("[login] Filling credentials ...")

    async def fill_and_submit():
        # Clear and fill phone
        phone_field = page.get_by_role("textbox", name="Phone number")
        await phone_field.wait_for(state="visible", timeout=10_000)
        await phone_field.click(click_count=3)   # select-all then replace
        await phone_field.press_sequentially(USERNAME, delay=50)

        # Fill password
        pwd_field = page.get_by_role("textbox", name="Password")
        await pwd_field.wait_for(state="visible", timeout=5_000)
        await pwd_field.fill(PASSWORD)
        await ss(page, "01_credentials_filled")

        # Click Sign in
        sign_in = page.get_by_role("button", name="Sign in")
        await sign_in.wait_for(state="visible", timeout=5_000)
        await sign_in.click()

        # Wait for redirect — either networkidle or URL change
        try:
            await page.wait_for_url(
                lambda url: "paymob.com" in url and "/login" not in url,
                timeout=15_000
            )
        except PwTimeout:
            await page.wait_for_timeout(4_000)

        await ss(page, "02_after_signin")
        if "paymob.com" not in page.url or "/login" in page.url:
            raise RuntimeError(f"[login] Login failed — URL: {page.url}")

    await retry_action(fill_and_submit, retries=3, delay=3.0, label="login/fill")
    print(f"[login] Logged in. URL: {page.url}")


# ---------------------------------------------------------------------------
# Navigate to Reports & Statements
# ---------------------------------------------------------------------------
async def navigate_to_reports(page) -> None:
    print("[nav] Navigating to Reports & Statements ...")
    await ss(page, "10_home")

    base = page.url.split("/home")[0]

    async def try_direct_url():
        for path in ["/reports", "/reports-statements", "/reports/statements"]:
            url = base + path
            print(f"[nav] Trying {url} ...")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            except PwTimeout:
                continue
            if "/login" not in page.url and base in page.url:
                # Confirm the reports page loaded — wait for the select or heading
                loaded = await wait_for_page_ready(
                    page,
                    'select, h1:has-text("Report"), h2:has-text("Report"), '
                    '[class*="report"], button:has-text("Generate")',
                    10_000
                )
                if loaded:
                    await ss(page, "11_reports_direct")
                    print(f"[nav] Landed on: {page.url}")
                    return True
        return False

    if await try_direct_url():
        return

    # Fallback: click the sidebar link
    print("[nav] Direct URL failed — clicking sidebar link ...")
    await page.goto(base + "/home/", wait_until="domcontentloaded", timeout=15_000)
    await page.wait_for_timeout(2_000)

    for label in ["Reports & Statements", "Reports", "Statements"]:
        loc = page.locator(
            f'a:has-text("{label}"), button:has-text("{label}"), span:has-text("{label}")'
        )
        if await loc.count() > 0:
            await loc.first.click()
            await wait_for_page_ready(
                page,
                'select, button:has-text("Generate")',
                10_000
            )
            await ss(page, "12_reports_clicked")
            print(f"[nav] Clicked '{label}'. URL: {page.url}")
            return

    raise RuntimeError(f"[nav] Could not find Reports & Statements. URL: {page.url}")


# ---------------------------------------------------------------------------
# KSA date picker helpers
# The KSA portal uses two SEPARATE rsuite DatePicker (rs-picker-date),
# one for "From" and one for "To". Each opens its own single-month calendar.
# ---------------------------------------------------------------------------

MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
          'July', 'August', 'September', 'October', 'November', 'December']


async def _close_any_open_picker(page) -> None:
    """Dismiss any open rsuite date picker calendar."""
    if await page.locator('.rs-calendar').count() > 0:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)


async def _get_calendar_month_year(page):
    """Read the month/year from the currently open single rsuite calendar."""
    return await page.evaluate(f"""() => {{
        const MONTHS = {MONTHS};
        const cal = document.querySelector('.rs-calendar');
        if (!cal) return null;
        const title = cal.querySelector(
            '.rs-calendar-header-title-date, .rs-calendar-header-title'
        );
        if (!title) return null;
        const txt = title.textContent || '';
        for (let i = 0; i < 12; i++) {{
            if (txt.includes(MONTHS[i])) {{
                const m = txt.match(/\\b(20\\d{{2}})\\b/);
                if (m) return [i + 1, parseInt(m[1])];
            }}
        }}
        return null;
    }}""")


async def _nav_calendar_to(page, target_month: int, target_year: int) -> None:
    """Navigate the open single-calendar to the given month/year."""
    for _ in range(24):
        cur = await _get_calendar_month_year(page)
        if cur and cur[0] == target_month and cur[1] == target_year:
            return
        if cur:
            diff = (cur[1] - target_year) * 12 + (cur[0] - target_month)
            btn_cls = '.rs-calendar-header-backward' if diff > 0 else '.rs-calendar-header-forward'
        else:
            btn_cls = '.rs-calendar-header-backward'
        btn = page.locator(btn_cls).first
        if await btn.count() > 0:
            await btn.click()
            await page.wait_for_timeout(300)
        else:
            print(f"[date] Nav button {btn_cls} not found")
            break


async def _click_day(page, day: int) -> bool:
    """Click a day cell in the open single rsuite calendar."""
    day_str = str(day)
    cal = page.locator('.rs-calendar').first
    cells = cal.locator(
        'td.rs-calendar-table-cell:not(.rs-calendar-table-cell-disabled) '
        '.rs-calendar-table-cell-day'
    )
    for i in range(await cells.count()):
        cell = cells.nth(i)
        if (await cell.text_content() or "").strip() == day_str:
            await cell.click(timeout=3_000)
            return True
    # Fallback JS click
    return bool(await cal.evaluate(f"""(cal) => {{
        for (const el of cal.querySelectorAll('span, div, td')) {{
            if ((el.textContent || '').trim() === '{day_str}' && el.offsetParent) {{
                el.click(); return true;
            }}
        }}
        return false;
    }}"""))


async def _open_picker_toggle(page, toggle_index: int) -> bool:
    """
    Click the Nth rs-picker-date toggle in the form area to open the calendar.
    Retries up to 3 times if the calendar doesn't appear after clicking.
    Returns True if calendar is visible.
    """
    for attempt in range(1, 4):
        # Re-query toggles each attempt (DOM may have re-rendered)
        toggles = page.locator('.rs-picker-date .rs-picker-toggle')
        cnt = await toggles.count()
        form_toggles = []
        for i in range(cnt):
            t = toggles.nth(i)
            bb = await t.bounding_box()
            if bb and 150 < bb["y"] < 500:
                form_toggles.append(t)

        if toggle_index >= len(form_toggles):
            print(f"[date] Toggle #{toggle_index} not found (found {len(form_toggles)} in form area)")
            await page.wait_for_timeout(1_000)
            continue

        print(f"[date]   Attempt {attempt}: clicking toggle #{toggle_index} ...")
        await form_toggles[toggle_index].click()

        # Wait up to 2s for the calendar to appear
        try:
            await page.wait_for_selector('.rs-calendar', state='visible', timeout=2_000)
            return True
        except PwTimeout:
            print(f"[date]   Calendar not visible after attempt {attempt} — retrying ...")
            await _close_any_open_picker(page)
            await page.wait_for_timeout(700)

    return False


async def _pick_single_date(page, dt, label: str, toggle_index: int) -> None:
    """Open the Nth rsuite DatePicker in the form area, navigate to dt, click day, OK."""
    print(f"[date] Opening '{label}' picker ...")

    opened = await _open_picker_toggle(page, toggle_index)
    await ss(page, f"22_picker_{label.lower()}_open")

    if not opened:
        print(f"[date] WARNING: could not open '{label}' picker — skipping date selection")
        return

    print(f"[date] Calendar opened for '{label}'. Navigating to {dt.month}/{dt.year} ...")
    await _nav_calendar_to(page, dt.month, dt.year)

    # Click the day (retry once if not found)
    ok = await _click_day(page, dt.day)
    if not ok:
        print(f"[date] Day {dt.day} not found on first try — re-navigating ...")
        await _nav_calendar_to(page, dt.month, dt.year)
        ok = await _click_day(page, dt.day)
    print(f"[date] Clicked day {dt.day}: {ok}")
    await page.wait_for_timeout(400)

    # Click OK / Apply to confirm
    confirmed = False
    for lbl in ["OK", "Apply", "Done", "Confirm"]:
        btn = page.locator(f'button:has-text("{lbl}")')
        if await btn.count() > 0:
            await btn.first.click()
            print(f"[date] Confirmed with '{lbl}'")
            confirmed = True
            # Wait for calendar to close
            try:
                await page.wait_for_selector('.rs-calendar', state='hidden', timeout=3_000)
            except PwTimeout:
                pass
            break
    if not confirmed:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)


async def _set_form_date_range(page) -> None:
    """Set the From and To dates using the two separate rsuite DatePicker components."""
    await _close_any_open_picker(page)
    await page.wait_for_timeout(300)
    await ss(page, "22a_before_form_picker")

    # Pick FROM date (toggle index 0 in form area)
    await _pick_single_date(page, START_DT, "From", toggle_index=0)
    await ss(page, "22c_from_set")

    # Pick TO date (toggle index 1 in form area)
    await _pick_single_date(page, END_DT, "To", toggle_index=1)
    await ss(page, "23_dates_set")


# ---------------------------------------------------------------------------
# Select Report Type
# ---------------------------------------------------------------------------
async def select_report_type(page) -> None:
    """Select 'Transactions' from the Report Type dropdown."""
    print("[report] Selecting Report Type: Transactions ...")

    # Wait for the select to be present and interactable
    await wait_for_page_ready(page, 'select', 10_000)

    async def do_select():
        selects = page.locator('select')
        cnt = await selects.count()
        for i in range(cnt):
            s = selects.nth(i)
            html = await s.inner_html()
            if "ransaction" in html.lower():
                await s.select_option(label="Transactions")
                print(f"[report] Selected 'Transactions' via native select [{i}]")
                await page.wait_for_timeout(500)
                return
        raise RuntimeError("[report] Report Type select not found")

    await retry_action(do_select, retries=3, delay=2.0, label="select-report-type")
    await ss(page, "21_type_selected")


# ---------------------------------------------------------------------------
# Generate Transactions report
# ---------------------------------------------------------------------------
async def generate_report(page) -> "Path | None":
    print(f"[report] Generating Transactions report {START_DATE} -> {END_DATE} ...")
    await ss(page, "20_reports_page")

    # --- Step 1: Select Report Type ---
    await select_report_type(page)

    # --- Step 2: Set date range ---
    print(f"[report] Setting date range {START_DATE} -> {END_DATE} ...")
    await _set_form_date_range(page)

    # --- Step 3: Click Generate ---
    print("[report] Clicking Generate Report ...")
    await _close_any_open_picker(page)
    await page.wait_for_timeout(500)

    # Log what the pickers are showing
    form_date_text = await page.evaluate("""() => {
        const toggles = Array.from(document.querySelectorAll('.rs-picker-date .rs-picker-toggle'));
        return toggles.map(t => (t.textContent || '').trim()).filter(Boolean).join(' -> ');
    }""")
    print(f"[report] Date pickers showing: {form_date_text}")

    # Wait for the Generate button to exist and be enabled
    gen_btn = page.locator('button:has-text("Generate Report"), button:has-text("Generate")')
    try:
        await gen_btn.first.wait_for(state="visible", timeout=10_000)
    except PwTimeout:
        await ss(page, "24_no_generate_btn")
        visible_btns = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('button'))
                .filter(b => b.offsetParent)
                .map(b => b.textContent.trim())
                .filter(t => t)
        """)
        print(f"[report] Visible buttons: {visible_btns}")
        raise RuntimeError("[report] Generate button not found")

    # JS click to bypass any overlay
    await gen_btn.first.evaluate("el => el.click()")
    print("[report] Generate clicked — waiting for new Pending row ...")
    await page.wait_for_timeout(3_000)
    await ss(page, "24_after_generate")

    # --- Step 4: Wait 60s, reload once, then find latest by Date Created ---
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    print("[report] Waiting 60s for report to generate ...")
    await page.wait_for_timeout(60_000)

    print("[report] Reloading page to get updated table ...")
    await page.goto(page.url, wait_until="domcontentloaded", timeout=20_000)
    await page.wait_for_timeout(3_000)
    await ss(page, "24b_ready_to_download")

    # --- Step 5: Find the row with the latest 'Date Created' timestamp ---
    dest_name = f"PAYMOB_KSA_TXN_{START_DATE}_to_{END_DATE}.csv"

    latest_btn_ref = await page.evaluate("""() => {
        // Rows: [From Date | To Date | Date Created | Type | Action]
        const rows = Array.from(document.querySelectorAll('table tbody tr'));
        const dlBtns = Array.from(document.querySelectorAll('button, a'))
            .filter(el => el.offsetParent !== null &&
                         (el.textContent || '').trim().toLowerCase() === 'download');

        let bestTs = -1, bestIdx = -1;
        rows.forEach((row, idx) => {
            const cells = row.querySelectorAll('td');
            if (cells.length < 3) return;
            const dateText = (cells[2]?.textContent || '').trim();
            if (!dateText) return;
            // Format: "21 May 2026, 6:25 AM"
            const d = new Date(dateText.replace(',', ''));
            if (!isNaN(d.getTime()) && d.getTime() > bestTs) {
                bestTs = d.getTime();
                bestIdx = idx;
            }
        });

        if (bestIdx === -1 || bestIdx >= dlBtns.length) {
            return { found: dlBtns.length > 0, idx: 0, dateText: 'unknown', fallback: true };
        }
        const cells = rows[bestIdx]?.querySelectorAll('td');
        return {
            found: true,
            idx: bestIdx,
            dateText: cells?.[2]?.textContent?.trim() || '',
            fallback: false
        };
    }""")

    print(f"[report] Latest report row: {latest_btn_ref}")

    async def do_download():
        # Re-query Download buttons and click the one at the identified index
        dl_buttons = page.locator('button:has-text("Download"), a:has-text("Download")')
        btn_count = await dl_buttons.count()
        if btn_count == 0:
            raise RuntimeError("[report] No Download buttons found")

        idx = latest_btn_ref.get("idx", 0) if latest_btn_ref.get("found") else 0
        # Clamp to valid range
        idx = min(idx, btn_count - 1)
        btn = dl_buttons.nth(idx)
        await btn.wait_for(state="visible", timeout=15_000)
        print(f"[report] Clicking Download button #{idx} "
              f"(Date Created: {latest_btn_ref.get('dateText', '?')})")

        async with page.expect_download(timeout=60_000) as dl_info:
            await btn.evaluate("el => el.click()")
        dl = await dl_info.value
        failure = await dl.failure()
        if failure:
            raise RuntimeError(f"[report] Download failed: {failure}")
        fname = dl.suggested_filename or dest_name
        dest  = DOWNLOAD_DIR / fname
        await dl.save_as(dest)
        return dest

    dest = await retry_action(do_download, retries=3, delay=3.0, label="download")
    print(f"[report] Saved: {dest.resolve()}")
    await ss(page, "25_download_done")
    return dest


# ---------------------------------------------------------------------------
# S3 Upload
# ---------------------------------------------------------------------------
def upload_to_s3(local_path: Path) -> str:
    s3_key = f"{S3_PREFIX}{local_path.name}"
    print(f"[s3] Uploading {local_path.name} -> s3://{S3_BUCKET}/{s3_key} ...")

    def _try_upload():
        s3 = boto3.client(
            "s3",
            region_name=S3_REGION,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
        content_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if local_path.suffix == ".xlsx" else "text/csv"
        )
        s3.upload_file(
            str(local_path), S3_BUCKET, s3_key,
            ExtraArgs={"ContentType": content_type},
        )
        return f"s3://{S3_BUCKET}/{s3_key}"

    for attempt in range(1, 4):
        try:
            uri = _try_upload()
            print(f"[s3] Upload complete -> {uri}")
            return uri
        except NoCredentialsError:
            print("[s3] ERROR: AWS credentials not found")
            raise
        except ClientError as e:
            code = e.response["Error"]["Code"]
            msg  = e.response["Error"]["Message"]
            if attempt < 3:
                print(f"[s3] Attempt {attempt} failed ({code}: {msg}). Retrying in 5s ...")
                import time; time.sleep(5)
            else:
                print(f"[s3] All upload attempts failed: {code} — {msg}")
                raise


# ---------------------------------------------------------------------------
# Main  (outer retry: up to 3 full-run attempts)
# ---------------------------------------------------------------------------
async def run_once(pw, is_ci: bool, slow_mo: int) -> Path:
    """One full attempt: launch browser, login, generate, download. Returns local file path."""
    browser = await pw.chromium.launch(headless=is_ci, slow_mo=slow_mo)
    context = await browser.new_context(
        accept_downloads=True,
        viewport={"width": 1440, "height": 900},
    )
    page = await context.new_page()
    try:
        await do_login(page)
        await navigate_to_reports(page)
        dest = await generate_report(page)
        if not dest or not dest.exists():
            raise RuntimeError("Download succeeded but file not found on disk")
        return dest
    except Exception:
        try:
            await ss(page, "error_final")
        except Exception:
            pass
        raise
    finally:
        await browser.close()


async def main() -> None:
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    print("=" * 60)
    print(f"[*] PayMob KSA Transactions Report")
    print(f"[*] Portal    : {LOGIN_URL}")
    print(f"[*] Username  : {USERNAME}")
    print(f"[*] Date range: {START_DATE}  ->  {END_DATE}")
    print(f"[*] S3 upload : {'enabled -> ' + S3_BUCKET + '/' + S3_PREFIX if S3_ENABLED else 'disabled'}")
    print("=" * 60)

    IS_CI   = os.environ.get("CI", "false").lower() == "true"
    SLOW_MO = 0 if IS_CI else 60

    MAX_ATTEMPTS = 3
    last_exc = None

    async with async_playwright() as pw:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                wait = 15 * (attempt - 1)
                print(f"\n{'='*60}")
                print(f"[*] FULL RETRY {attempt}/{MAX_ATTEMPTS} — waiting {wait}s before restart ...")
                print(f"{'='*60}\n")
                await asyncio.sleep(wait)

            try:
                dest = await run_once(pw, IS_CI, SLOW_MO)
                print(f"\n[+] Downloaded: {dest.resolve()}")

                if S3_ENABLED:
                    s3_uri = upload_to_s3(dest)
                    print(f"[+] S3: {s3_uri}")
                else:
                    print("[s3] S3 upload disabled.")

                print(f"\n[+] Done in {attempt} attempt(s).")
                return  # success — exit

            except Exception as exc:
                last_exc = exc
                print(f"\n[!] Attempt {attempt}/{MAX_ATTEMPTS} failed: {exc}")
                if attempt < MAX_ATTEMPTS:
                    print("[!] Will retry from scratch ...")
                else:
                    print("[!] All attempts exhausted.")

    # All attempts failed
    raise RuntimeError(
        f"PayMob KSA export failed after {MAX_ATTEMPTS} attempts. "
        f"Last error: {last_exc}"
    ) from last_exc


if __name__ == "__main__":
    asyncio.run(main())

import os
import re
import ssl
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

VFS_URL = os.getenv("VFS_BOOKING_URL", "https://services.vfsglobal.com/usa/en/ind/")
USERNAME = os.getenv("VFS_USERNAME", "")
PASSWORD = os.getenv("VFS_PASSWORD", "")
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")
GMAIL_USERNAME = os.getenv("GMAIL_USERNAME", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# Change only these if VFS changes its UI.
LOGIN_SELECTORS = [
    "input[type='email']",
    "input[name*='email' i]",
    "input[id*='email' i]",
    "input[name*='username' i]",
]
PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[name*='password' i]",
    "input[id*='password' i]",
]
LOGIN_BUTTON_SELECTORS = [
    "button:has-text('Login')",
    "button:has-text('Sign in')",
    "input[type='submit']",
    "a:has-text('Login')",
]

NO_SLOT_PATTERNS = [
    r"no\s+(appointment|slot)s?\s+(available|found)",
    r"no\s+available\s+(appointment|slot)s?",
    r"currently\s+no\s+(appointment|slot)s?",
    r"no\s+slots?\s+available",
    r"fully\s+booked",
]

POSITIVE_PATTERNS = [
    r"\bavailable\b",
    r"\bappointment\b",
    r"\bslot\b",
    r"\bcalendar\b",
]

def first_visible(page, selectors):
    for s in selectors:
        try:
            loc = page.locator(s).first
            if loc.is_visible(timeout=1200):
                return loc
        except Exception:
            pass
    return None

def click_first(page, selectors):
    for s in selectors:
        try:
            loc = page.locator(s).first
            if loc.is_visible(timeout=1200):
                loc.click()
                return True
        except Exception:
            pass
    return False

def send_email(subject, body, screenshot=None):
    if not all([ALERT_EMAIL, GMAIL_USERNAME, GMAIL_APP_PASSWORD]):
        raise RuntimeError("Missing Gmail alert secrets")

    msg = EmailMessage()
    msg["From"] = GMAIL_USERNAME
    msg["To"] = ALERT_EMAIL
    msg["Subject"] = subject
    msg.set_content(body)

    if screenshot and Path(screenshot).exists():
        data = Path(screenshot).read_bytes()
        msg.add_attachment(data, maintype="image", subtype="png",
                           filename="vfs-status.png")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
        smtp.login(GMAIL_USERNAME, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)

def login_if_needed(page):
    # The current VFS site may use a registration/account flow whose exact
    # selectors can change. First inspect whether a password field is present.
    pw = first_visible(page, PASSWORD_SELECTORS)
    if not pw:
        # Try opening an account/login link if one is visible.
        click_first(page, [
            "a:has-text('Existing Users')",
            "a:has-text('Login')",
            "a:has-text('Sign in')",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
        ])
        page.wait_for_timeout(2000)
        pw = first_visible(page, PASSWORD_SELECTORS)

    if not pw:
        return False

    email = first_visible(page, LOGIN_SELECTORS)
    if email:
        email.fill(USERNAME)
    pw.fill(PASSWORD)
    if not click_first(page, LOGIN_BUTTON_SELECTORS):
        pw.press("Enter")
    page.wait_for_timeout(3500)
    return True

def classify(page):
    text = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=10000)).strip()
    low = text.lower()

    # Hard negative first.
    if any(re.search(p, low) for p in NO_SLOT_PATTERNS):
        return False, "No appointment/slot language detected"

    # Look for date/time/calendar structures commonly shown by appointment pages.
    date_like = bool(re.search(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b"
        r"|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", low))
    time_like = bool(re.search(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", low))
    calendar_like = "calendar" in low or "select date" in low or "select time" in low

    # Strong signal: date/time/calendar plus appointment/slot context.
    if (date_like and (time_like or calendar_like)) and any(
        re.search(p, low) for p in POSITIVE_PATTERNS
    ):
        return True, "Date/time/calendar content detected"

    # If the page exposes selectable buttons with date/time labels.
    try:
        buttons = page.locator("button, [role='button'], a").all_inner_texts()
        useful = [x.strip() for x in buttons if x.strip()]
        selectable = [x for x in useful if re.search(
            r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b|\b\d{1,2}:\d{2}\b",
            x.lower())]
        if selectable:
            return True, "Selectable date/time controls detected"
    except Exception:
        pass

    return False, "No clear available-slot signal"

def main():
    Path("artifacts").mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()
        page.goto(VFS_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        logged = login_if_needed(page)
        if logged:
            page.wait_for_timeout(2500)

        # Save diagnostic evidence on every run; workflow uploads it only on failure/alert.
        page.screenshot(path="artifacts/vfs-status.png", full_page=True)
        Path("artifacts/vfs-status.html").write_text(page.content(), encoding="utf-8")

        found, reason = classify(page)

        print(f"VFS URL: {VFS_URL}")
        print(f"Login form handled: {logged}")
        print(f"Appointment signal: {found}")
        print(f"Reason: {reason}")
        print(f"Final URL: {page.url}")

        if found:
            subject = "🚨 VFS India Passport Appointment May Be Available"
            body = (
                "A possible appointment/calendar slot was detected for the VFS India "
                "passport appointment monitor.\n\n"
                f"Reason: {reason}\n"
                f"Page: {page.url}\n\n"
                "Open the VFS page and verify the slot manually. The monitor does not "
                "book appointments automatically.\n"
            )
            send_email(subject, body, "artifacts/vfs-status.png")
            print("Alert email sent.")
        browser.close()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
VFS India passport appointment monitor.

Logs in, walks the booking flow defined in vfs_config.json, and emails you when
a slot appears. It never books anything. It also emails you when it has been
blocked or the site layout has changed, so a broken monitor can't quietly look
like "no slots".

Usage:
  python monitor.py                    # normal run (what GitHub Actions does)
  python monitor.py --dry-run          # log alerts instead of emailing
  python monitor.py --headed --pause   # watch it locally; opens Playwright Inspector at the end
  python monitor.py --test-email       # send a test email and exit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from email.message import EmailMessage
from enum import Enum
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

log = logging.getLogger("vfs-monitor")

DEFAULT_URL = "https://services.vfsglobal.com/usa/en/ind/"
ARTIFACT_DIR = Path("artifacts")
SCREENSHOT = ARTIFACT_DIR / "vfs-status.png"
PAGE_HTML = ARTIFACT_DIR / "vfs-status.html"
CAPTCHA_FRAME_HINTS = ("challenges.cloudflare.com", "recaptcha", "hcaptcha.com")
STEP_ACTIONS = {
    "click": ("selector",),
    "select": ("selector", "option"),
    "wait_for": ("selector",),
    "goto": ("url",),
    "sleep": ("ms",),
}


class ConfigError(Exception):
    """Missing or invalid configuration; the run cannot proceed."""


class Status(str, Enum):
    AVAILABLE = "AVAILABLE"
    NO_SLOTS = "NO_SLOTS"
    BLOCKED = "BLOCKED"
    LOGIN_FAILED = "LOGIN_FAILED"
    NAV_FAILED = "NAV_FAILED"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


HEALTHY = {Status.AVAILABLE, Status.NO_SLOTS}

ADVICE = {
    Status.BLOCKED: "VFS/Cloudflare is blocking the automated browser. GitHub's datacenter "
    "IPs are often blocked; running the monitor from your own machine usually works better.",
    Status.LOGIN_FAILED: "Check VFS_USERNAME / VFS_PASSWORD. If VFS now requires an OTP or "
    "captcha at login, the monitor can't get past it.",
    Status.NAV_FAILED: "A navigation step failed, which usually means VFS changed its page "
    "layout. Update the steps in vfs_config.json using the screenshot.",
    Status.UNKNOWN: "The page loaded but matched neither the slot nor the no-slot patterns. "
    "Check the screenshot and update slot_patterns / no_slot_patterns in vfs_config.json.",
    Status.ERROR: "The browser hit a timeout or error. Occasional errors are normal; "
    "repeated ones need a look.",
}


@dataclass
class Result:
    status: Status
    reason: str
    detail: str = ""
    url: str = ""


# --------------------------------------------------------------------------- settings

def env(name: str, default: str = "") -> str:
    """Read an env var, treating empty as unset (GitHub passes unset vars/secrets as '')."""
    return os.environ.get(name, "").strip() or default


def env_int(name: str, default: int) -> int:
    raw = env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ConfigError(f"{name} must be at least 1, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    url: str
    username: str
    password: str
    alert_email: str
    gmail_username: str
    gmail_app_password: str
    config_path: Path
    state_path: Path
    slot_repeat_minutes: int
    degraded_after_runs: int
    degraded_repeat_hours: int

    @classmethod
    def from_env(cls, need_vfs: bool, need_email: bool) -> "Settings":
        s = cls(
            url=env("VFS_BOOKING_URL", DEFAULT_URL),
            username=env("VFS_USERNAME"),
            password=os.environ.get("VFS_PASSWORD", ""),  # not stripped: spaces may be real
            alert_email=env("ALERT_EMAIL"),
            gmail_username=env("GMAIL_USERNAME"),
            gmail_app_password=env("GMAIL_APP_PASSWORD").replace(" ", ""),
            config_path=Path(env("VFS_CONFIG", "vfs_config.json")),
            state_path=Path(env("VFS_STATE", "state/state.json")),
            slot_repeat_minutes=env_int("SLOT_ALERT_REPEAT_MINUTES", 60),
            degraded_after_runs=env_int("DEGRADED_AFTER_RUNS", 3),
            degraded_repeat_hours=env_int("DEGRADED_REPEAT_HOURS", 12),
        )
        required: dict[str, str] = {}
        if need_vfs:
            required |= {"VFS_USERNAME": s.username, "VFS_PASSWORD": s.password}
        if need_email:
            required |= {
                "ALERT_EMAIL": s.alert_email,
                "GMAIL_USERNAME": s.gmail_username,
                "GMAIL_APP_PASSWORD": s.gmail_app_password,
            }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigError("Missing required settings: " + ", ".join(missing))
        if not s.url.startswith("https://"):
            raise ConfigError(f"VFS_BOOKING_URL must start with https://, got {s.url!r}")
        return s


def load_config(path: Path) -> dict[str, Any]:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path} must contain a JSON object")

    list_keys = ("dismiss_selectors", "steps", "slot_patterns", "no_slot_patterns",
                 "slot_selectors", "block_patterns", "otp_patterns")
    for key in list_keys:
        if not isinstance(cfg.get(key), list):
            raise ConfigError(f"Config key {key!r} must be a list")
    login = cfg.get("login")
    if not isinstance(login, dict):
        raise ConfigError("Config key 'login' must be an object")
    for key in ("open_login", "username", "password", "submit", "logged_in", "error"):
        if not isinstance(login.get(key), list):
            raise ConfigError(f"Config key login.{key} must be a list")
    for key in ("username", "password", "logged_in"):
        if not login[key]:
            raise ConfigError(f"Config key login.{key} must not be empty")

    for key in ("slot_patterns", "no_slot_patterns", "block_patterns", "otp_patterns"):
        for pattern in cfg[key]:
            try:
                re.compile(pattern)
            except (re.error, TypeError) as exc:
                raise ConfigError(f"Bad regex in {key}: {pattern!r} ({exc})") from exc

    steps = [st for st in cfg["steps"] if isinstance(st, dict) and not st.get("disabled")]
    for i, step in enumerate(steps, 1):
        action = step.get("action")
        if action not in STEP_ACTIONS:
            raise ConfigError(f"Step {i}: unknown action {action!r} (use one of {sorted(STEP_ACTIONS)})")
        for field_name in STEP_ACTIONS[action]:
            if step.get(field_name) in (None, ""):
                raise ConfigError(f"Step {i} ({action}) is missing {field_name!r}")
    cfg["steps"] = steps
    return cfg


# --------------------------------------------------------------------------- page helpers

def one_line(value: object, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def any_visible(page: Page, selectors: list[str]) -> Locator:
    loc = page.locator(selectors[0])
    for sel in selectors[1:]:
        loc = loc.or_(page.locator(sel))
    return loc.filter(visible=True).first


def wait_visible(page: Page, selectors: list[str], timeout_ms: int) -> Locator | None:
    if not selectors:
        return None
    loc = any_visible(page, selectors)
    try:
        loc.wait_for(state="visible", timeout=timeout_ms)
        return loc
    except PlaywrightTimeoutError:
        return None


def visible_now(page: Page, selectors: list[str]) -> bool:
    if not selectors:
        return False
    try:
        return any_visible(page, selectors).count() > 0
    except PlaywrightError:
        return False


def settle(page: Page, extra_ms: int = 1000) -> None:
    """Give the Angular SPA time to finish rendering."""
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        pass  # some pages poll forever; the fixed wait below still applies
    page.wait_for_timeout(extra_ms)


def page_text(page: Page) -> str:
    try:
        text = page.locator("body").inner_text(timeout=10_000)
    except PlaywrightError:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def first_match(patterns: list[str], text: str) -> re.Match[str] | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match
    return None


def detect_block(page: Page, cfg: dict[str, Any], text: str | None = None,
                 check_frames: bool = False) -> Result | None:
    text = page_text(page) if text is None else text
    match = first_match(cfg["block_patterns"], text)
    if match:
        return Result(Status.BLOCKED, f"Block/challenge page detected ({match.group(0)!r})")
    if check_frames:
        for frame in page.frames:
            if any(hint in frame.url for hint in CAPTCHA_FRAME_HINTS):
                return Result(Status.BLOCKED, "Captcha present and not passed")
    return None


def dismiss_overlays(page: Page, cfg: dict[str, Any]) -> None:
    for sel in cfg["dismiss_selectors"]:
        loc = page.locator(sel).filter(visible=True).first
        try:
            if loc.count():
                loc.click(timeout=3_000)
                page.wait_for_timeout(500)
        except PlaywrightError:
            pass


# --------------------------------------------------------------------------- flow

def login(page: Page, cfg: dict[str, Any], s: Settings) -> Result | None:
    """Return None on success (or already logged in), else a failure Result."""
    lg = cfg["login"]
    if visible_now(page, lg["logged_in"]):
        log.info("Already logged in")
        return None

    password = wait_visible(page, lg["password"], 5_000)
    if password is None:
        opener = wait_visible(page, lg["open_login"], 3_000)
        if opener is not None:
            opener.click()
            settle(page)
        password = wait_visible(page, lg["password"], 10_000)
    if password is None:
        if visible_now(page, lg["logged_in"]):
            return None
        return (detect_block(page, cfg, check_frames=True)
                or Result(Status.LOGIN_FAILED, "Could not find the login form"))

    username = wait_visible(page, lg["username"], 3_000)
    if username is None:
        return Result(Status.LOGIN_FAILED, "Found a password field but no username/email field")
    username.fill(s.username)
    password.fill(s.password)

    submit = wait_visible(page, lg["submit"], 3_000)
    if submit is None:
        password.press("Enter")
    elif submit.is_enabled():
        submit.click()
    else:
        return (detect_block(page, cfg, check_frames=True)
                or Result(Status.LOGIN_FAILED, "Sign-in button is disabled (captcha not solved?)"))

    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        page.wait_for_timeout(1_000)
        if visible_now(page, lg["logged_in"]):
            settle(page)
            log.info("Login succeeded")
            return None
        text = page_text(page)
        blocked = detect_block(page, cfg, text)
        if blocked:
            return blocked
        if not visible_now(page, lg["password"]) and first_match(cfg["otp_patterns"], text):
            return Result(Status.LOGIN_FAILED, "VFS is asking for an OTP/verification code")

    error_text = ""
    if visible_now(page, lg["error"]):
        try:
            error_text = one_line(any_visible(page, lg["error"]).inner_text(timeout=2_000), 150)
        except PlaywrightError:
            pass
    if error_text:
        return Result(Status.LOGIN_FAILED, f"Login rejected: {error_text}")
    return (detect_block(page, cfg, check_frames=True)
            or Result(Status.LOGIN_FAILED, "Still not logged in 25s after submitting credentials"))


def choose_option(page: Page, selector: str, option: str) -> None:
    field = page.locator(selector).filter(visible=True).first
    field.wait_for(state="visible")
    if field.evaluate("el => el.tagName.toLowerCase()") == "select":
        field.select_option(label=option)
        return
    field.click()  # Angular Material / custom dropdown
    page.locator("mat-option, [role='option']").filter(has_text=option).filter(
        visible=True).first.click()


def run_steps(page: Page, cfg: dict[str, Any]) -> Result | None:
    for i, step in enumerate(cfg["steps"], 1):
        action = step["action"]
        name = step.get("name") or f"step {i}"
        target = step.get("selector") or step.get("url") or ""
        log.info("Step %d: %s", i, name)
        try:
            if action == "click":
                page.locator(step["selector"]).filter(visible=True).first.click()
            elif action == "select":
                choose_option(page, step["selector"], str(step["option"]))
            elif action == "wait_for":
                page.locator(step["selector"]).filter(visible=True).first.wait_for(
                    state="visible", timeout=int(step.get("timeout_ms", 20_000)))
            elif action == "goto":
                page.goto(step["url"], wait_until="domcontentloaded")
            elif action == "sleep":
                page.wait_for_timeout(int(step["ms"]))
            if action != "sleep":
                settle(page, int(step.get("settle_ms", 1_500)))
        except PlaywrightTimeoutError:
            return Result(Status.NAV_FAILED, f"Step {i} '{name}' timed out ({action} {target!r})")
        except PlaywrightError as exc:
            return Result(Status.NAV_FAILED, f"Step {i} '{name}' failed: {one_line(exc, 150)}")
    return None


def classify(page: Page, cfg: dict[str, Any]) -> Result:
    text = page_text(page)
    blocked = detect_block(page, cfg, text)
    if blocked:
        return blocked

    # Specific "slots exist" wording wins; then explicit "no slots"; then calendar controls.
    match = first_match(cfg["slot_patterns"], text)
    if match:
        return Result(Status.AVAILABLE, "Slot wording found on page", one_line(match.group(0)))

    match = first_match(cfg["no_slot_patterns"], text)
    if match:
        return Result(Status.NO_SLOTS, f"No-slot message found ({match.group(0)!r})")

    for sel in cfg["slot_selectors"]:
        loc = page.locator(sel).filter(visible=True)
        try:
            count = loc.count()
            if count:
                labels = [one_line(t, 40) for t in loc.all_inner_texts()[:10] if t.strip()]
                return Result(Status.AVAILABLE, f"{count} selectable slot element(s) ({sel})",
                              ", ".join(labels))
        except PlaywrightError:
            continue

    reason = "Target page matched neither slot nor no-slot patterns"
    if not cfg["steps"]:
        reason += " (no navigation steps configured in vfs_config.json)"
    return Result(Status.UNKNOWN, reason)


def check(page: Page, cfg: dict[str, Any], s: Settings) -> Result:
    log.info("Opening %s", s.url)
    response = page.goto(s.url, wait_until="domcontentloaded")
    settle(page, 2_000)

    if detect_block(page, cfg):
        # A browser challenge sometimes clears on its own after a few seconds.
        page.wait_for_timeout(8_000)
        settle(page)
        blocked = detect_block(page, cfg)
        if blocked:
            return blocked
    elif response is not None and response.status >= 400:
        status = Status.BLOCKED if response.status in (403, 429) else Status.ERROR
        return Result(status, f"HTTP {response.status} from the landing page")

    dismiss_overlays(page, cfg)
    failure = login(page, cfg, s)
    if failure:
        return failure
    dismiss_overlays(page, cfg)

    failure = run_steps(page, cfg)
    if failure:
        return detect_block(page, cfg) or failure
    return classify(page, cfg)


def save_evidence(page: Page) -> None:
    try:
        page.screenshot(path=str(SCREENSHOT), full_page=True, timeout=15_000)
    except PlaywrightError as exc:
        log.warning("Screenshot failed: %s", one_line(exc, 150))
    try:
        PAGE_HTML.write_text(page.content(), encoding="utf-8")
    except (PlaywrightError, OSError) as exc:
        log.warning("Saving page HTML failed: %s", one_line(exc, 150))


def run_browser(s: Settings, cfg: dict[str, Any], headed: bool, pause: bool) -> Result:
    ARTIFACT_DIR.mkdir(exist_ok=True)
    for stale in (SCREENSHOT, PAGE_HTML):
        stale.unlink(missing_ok=True)  # never attach an old screenshot to a new alert

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        try:
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="en-US",
                timezone_id="America/New_York",
            )
            context.set_default_timeout(int(cfg.get("default_timeout_ms", 20_000)))
            context.set_default_navigation_timeout(45_000)
            page = context.new_page()
            try:
                result = check(page, cfg, s)
            except PlaywrightTimeoutError as exc:
                result = Result(Status.ERROR, f"Timed out: {one_line(exc, 200)}")
            except PlaywrightError as exc:
                result = Result(Status.ERROR, f"Browser error: {one_line(exc, 200)}")
            result.url = page.url
            save_evidence(page)
            if pause:
                page.pause()
            return result
        finally:
            browser.close()


# --------------------------------------------------------------------------- state & alerts

@dataclass
class State:
    last_run_at: float = 0.0
    last_status: str = ""
    last_reason: str = ""
    consecutive_bad: int = 0
    slot_fingerprint: str = ""
    slot_alert_at: float = 0.0
    degraded_active: bool = False
    degraded_alert_at: float = 0.0

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("State file unreadable (%s); starting fresh", exc)
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)


@dataclass
class Alert:
    kind: str  # slot | degraded | recovered | test
    subject: str
    body: str
    attach_screenshot: bool = True


def fingerprint(result: Result) -> str:
    return hashlib.sha256((result.detail or result.reason).encode()).hexdigest()[:16]


def run_link() -> str:
    server, repo, run_id = env("GITHUB_SERVER_URL"), env("GITHUB_REPOSITORY"), env("GITHUB_RUN_ID")
    return f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else "(local run)"


def plan_alerts(state: State, r: Result, s: Settings, now: float) -> list[Alert]:
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    facts = (f"Status: {r.status.value}\nReason: {r.reason}\n"
             + (f"Detail: {r.detail}\n" if r.detail else "")
             + f"Page: {r.url or s.url}\nChecked: {stamp}\nRun: {run_link()}\n")
    alerts: list[Alert] = []

    if r.status is Status.AVAILABLE:
        new_slot = fingerprint(r) != state.slot_fingerprint
        repeat_due = now - state.slot_alert_at >= s.slot_repeat_minutes * 60
        if new_slot or repeat_due:
            alerts.append(Alert(
                "slot",
                "🚨 VFS India passport appointment may be available",
                "A possible appointment slot was detected.\n\n" + facts
                + "\nLog in and book it manually. This monitor never books.\n",
            ))

    if r.status in HEALTHY:
        if state.degraded_active:
            alerts.append(Alert("recovered", "✅ VFS monitor is working again",
                                "The monitor can read the booking page again.\n\n" + facts,
                                attach_screenshot=False))
    else:
        bad_runs = state.consecutive_bad + 1
        repeat_due = now - state.degraded_alert_at >= s.degraded_repeat_hours * 3600
        if bad_runs >= s.degraded_after_runs and (not state.degraded_active or repeat_due):
            alerts.append(Alert(
                "degraded",
                f"⚠️ VFS monitor can't check slots ({r.status.value})",
                f"The last {bad_runs} checks failed, so the monitor is NOT currently "
                "watching for slots.\n\n" + facts + "\n" + ADVICE.get(r.status, "")
                + "\n\nThe screenshot and page HTML are attached to the workflow run.\n",
            ))
    return alerts


def apply_result(state: State, r: Result, now: float, sent: set[str]) -> None:
    state.last_run_at = now
    state.last_status = r.status.value
    state.last_reason = r.reason
    state.consecutive_bad = 0 if r.status in HEALTHY else state.consecutive_bad + 1
    if r.status is Status.NO_SLOTS:
        state.slot_fingerprint = ""  # so the next slot that appears alerts immediately
    if "slot" in sent:
        state.slot_fingerprint = fingerprint(r)
        state.slot_alert_at = now
    if "recovered" in sent:
        state.degraded_active = False
    if "degraded" in sent:
        state.degraded_active = True
        state.degraded_alert_at = now


class Mailer:
    def __init__(self, s: Settings, dry_run: bool) -> None:
        self.s = s
        self.dry_run = dry_run

    def send(self, alert: Alert) -> None:
        if self.dry_run:
            log.info("[dry-run] would email %r:\n%s", alert.subject, alert.body)
            return
        msg = EmailMessage()
        msg["From"] = self.s.gmail_username
        msg["To"] = self.s.alert_email
        msg["Subject"] = alert.subject
        msg.set_content(alert.body)
        if alert.attach_screenshot and SCREENSHOT.exists():
            msg.add_attachment(SCREENSHOT.read_bytes(), maintype="image", subtype="png",
                               filename=SCREENSHOT.name)

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30,
                                      context=ssl.create_default_context()) as smtp:
                    smtp.login(self.s.gmail_username, self.s.gmail_app_password)
                    smtp.send_message(msg)
                log.info("Sent %s email", alert.kind)
                return
            except smtplib.SMTPAuthenticationError:
                raise  # retrying won't fix a bad app password
            except (smtplib.SMTPException, OSError) as exc:
                last_error = exc
                log.warning("Email attempt %d failed: %s", attempt, exc)
                time.sleep(5 * attempt)
        raise RuntimeError(f"Could not send email after 3 attempts: {last_error}")


# --------------------------------------------------------------------------- GitHub glue

def gh_output(**values: str) -> None:
    path = env("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in values.items():
                fh.write(f"{key}={value}\n")


def gh_summary(r: Result, state: State, sent: set[str]) -> None:
    path = env("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("| Field | Value |\n|---|---|\n")
        fh.write(f"| Status | `{r.status.value}` |\n")
        fh.write(f"| Reason | {one_line(r.reason).replace('|', '/')} |\n")
        fh.write(f"| Consecutive failed checks | {state.consecutive_bad} |\n")
        fh.write(f"| Emails sent | {', '.join(sorted(sent)) or 'none'} |\n")


# --------------------------------------------------------------------------- main

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="VFS India passport appointment monitor")
    p.add_argument("--dry-run", action="store_true", help="log alerts instead of emailing")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--pause", action="store_true",
                   help="open Playwright Inspector before closing (use with --headed)")
    p.add_argument("--test-email", action="store_true", help="send a test email and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.pause and not args.headed:
        log.warning("--pause only works with --headed; ignoring it")
        args.pause = False

    try:
        s = Settings.from_env(need_vfs=not args.test_email, need_email=not args.dry_run)
        cfg = load_config(s.config_path)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2

    mailer = Mailer(s, dry_run=args.dry_run)
    if args.test_email:
        mailer.send(Alert("test", "VFS monitor test email",
                          "If you can read this, alerts are working.\n", attach_screenshot=False))
        return 0

    state = State.load(s.state_path)
    now = time.time()
    result = run_browser(s, cfg, headed=args.headed, pause=args.pause)
    log.info("Result: %s | %s%s", result.status.value, result.reason,
             f" | {result.detail}" if result.detail else "")

    sent: set[str] = set()
    exit_code = 0
    for alert in plan_alerts(state, result, s, now):
        try:
            mailer.send(alert)
            sent.add(alert.kind)
        except Exception as exc:  # noqa: BLE001 - any mail failure must fail the job
            log.error("Failed to send %s email: %s", alert.kind, exc)
            exit_code = 1  # GitHub's own failure email becomes the fallback alert

    apply_result(state, result, now, sent)
    state.save(s.state_path)
    gh_output(status=result.status.value,
              diagnostics=str(result.status not in HEALTHY).lower())
    gh_summary(result, state, sent)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

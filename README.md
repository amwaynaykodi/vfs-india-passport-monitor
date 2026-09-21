# VFS India Passport Appointment Monitor

Monitors the VFS India USA passport appointment flow and emails you when an appointment/calendar slot appears.

**Designed for:** Atlanta ICAC / Indian passport re-issue, but the URL and keywords are configurable.

## Important
- This monitor checks and alerts; it does **not** book appointments.
- VFS may present CAPTCHA/anti-bot checks. If that happens, GitHub's cloud runner cannot complete the check automatically. The workflow will save a screenshot/page HTML as an artifact.
- Never commit passwords, app passwords, cookies, or Playwright storage state.
- GitHub scheduled workflows run at a minimum configured interval of 5 minutes and can be delayed by GitHub. This is not a guaranteed exact 5-minute SLA.

## Files
- `monitor.py` — Playwright monitor + Gmail SMTP alert
- `.github/workflows/monitor.yml` — runs every 5 minutes
- `requirements.txt` — dependency

## GitHub Secrets
Add these repository secrets:
- `VFS_USERNAME` — your VFS account email
- `VFS_PASSWORD` — your VFS password
- `ALERT_EMAIL` — address that should receive alerts
- `GMAIL_USERNAME` — Gmail address used to send alerts
- `GMAIL_APP_PASSWORD` — 16-character Google App Password

If the VFS page does not use the selectors in `monitor.py`, run it locally once and adjust the selector section.

## Local test
On Windows PowerShell:
```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
$env:VFS_USERNAME="..."
$env:VFS_PASSWORD="..."
$env:ALERT_EMAIL="..."
$env:GMAIL_USERNAME="..."
$env:GMAIL_APP_PASSWORD="..."
$env:VFS_BOOKING_URL="https://services.vfsglobal.com/usa/en/ind/"
python monitor.py
```

The local test is useful because VFS can change the login/calendar UI.

## GitHub
Create a public repository, upload these files, add the five secrets, enable Actions, then run the workflow manually once. After that it runs on schedule.

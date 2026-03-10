"""Optional email sending via AWS SES. Triggered only when EMAIL_ENABLED=true and targets_changed > 0."""

import os


def _bool_env(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes")


def _str_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


EMAIL_ENABLED = _bool_env("EMAIL_ENABLED", False) or _bool_env("SEND_EMAIL", False)
DRY_RUN = _bool_env("DRY_RUN", False)
SES_REGION = _str_env("SES_REGION", "us-east-1")
FROM_EMAIL = _str_env("FROM_EMAIL", "")
TO_EMAILS = [e.strip() for e in _str_env("TO_EMAILS", "").split(",") if e.strip()]
EMAIL_SUBJECT_PREFIX = _str_env("EMAIL_SUBJECT_PREFIX", "[Web Change Report]")
ENVIRONMENT = _str_env("ENVIRONMENT", "")


def send_report(report: str, targets_changed: int) -> bool:
    """
    Send the report via SES when EMAIL_ENABLED=true and targets_changed > 0.
    Returns True if sent (or would have sent in dry-run), False otherwise.
    """
    if not EMAIL_ENABLED or targets_changed <= 0:
        return False

    if not FROM_EMAIL or not TO_EMAILS:
        print("[emailer] Skipping: FROM_EMAIL and TO_EMAILS must be set when EMAIL_ENABLED=true")
        return False

    prefix = EMAIL_SUBJECT_PREFIX or "[Web Change Report]"
    subject = f"{prefix} [{ENVIRONMENT}] - Changes Detected" if ENVIRONMENT else f"{prefix} - Changes Detected"
    body_text = report

    if DRY_RUN:
        print("[emailer] DRY_RUN=true - would send email (targets_changed=%d):" % targets_changed)
        print(f"  Subject: {subject}")
        print(f"  From: {FROM_EMAIL}")
        print(f"  To: {TO_EMAILS}")
        print("  Body:")
        print("-" * 40)
        print(body_text)
        print("-" * 40)
        return True

    try:
        import boto3
        client = boto3.client("ses", region_name=SES_REGION)
    except Exception as e:
        print(f"[emailer] Failed to create SES client: {e}")
        return False

    # Send to each recipient individually so one unverified/invalid address
    # does not block delivery to the others (SES rejects the entire call if
    # any recipient is unverified in sandbox mode).
    any_sent = False
    for recipient in TO_EMAILS:
        try:
            client.send_email(
                Source=FROM_EMAIL,
                Destination={"ToAddresses": [recipient]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body_text, "Charset": "UTF-8"}},
                },
            )
            print(f"[emailer] Sent report to {recipient}")
            any_sent = True
        except Exception as e:
            print(f"[emailer] Failed to send to {recipient}: {e}")

    return any_sent

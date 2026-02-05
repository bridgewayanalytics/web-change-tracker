"""Optional email sending via AWS SES. Triggered only when changes are detected."""

import os


def _bool_env(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes")


def _str_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


SEND_EMAIL = _bool_env("SEND_EMAIL", False)
DRY_RUN = _bool_env("DRY_RUN", False)
SES_REGION = _str_env("SES_REGION", "us-east-1")
FROM_EMAIL = _str_env("FROM_EMAIL", "")
TO_EMAILS = [e.strip() for e in _str_env("TO_EMAILS", "").split(",") if e.strip()]


def send_report(report: str, has_changes: bool) -> bool:
    """
    Send the report via SES when changes are detected and SEND_EMAIL is true.
    Returns True if sent (or would have sent in dry-run), False otherwise.
    """
    if not has_changes:
        return False

    if not SEND_EMAIL:
        return False

    if not FROM_EMAIL or not TO_EMAILS:
        print("[emailer] Skipping: FROM_EMAIL and TO_EMAILS must be set when SEND_EMAIL=true")
        return False

    subject = "Web Change Report - Changes Detected"
    body_text = report

    if DRY_RUN:
        print("[emailer] DRY_RUN=true - would send email:")
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
        client.send_email(
            Source=FROM_EMAIL,
            Destination={"ToAddresses": TO_EMAILS},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body_text, "Charset": "UTF-8"}},
            },
        )
        print(f"[emailer] Sent report to {TO_EMAILS}")
        return True
    except Exception as e:
        print(f"[emailer] Failed to send: {e}")
        return False

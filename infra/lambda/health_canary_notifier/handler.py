import os
import boto3
from datetime import datetime, timezone

SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]


def handler(event, context):
    sns = boto3.client("sns")
    now = datetime.now(timezone.utc).strftime("%A, %B %d %Y at %H:%M UTC")

    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject="[Automated Test] Web Change Tracker: Health Alert Delivery Check",
        Message=(
            "This is a scheduled canary notification sent twice weekly (Tuesday and Friday)\n"
            "to confirm that health alert emails are being delivered to your inbox.\n\n"
            "No action is required — the pipeline is running normally.\n\n"
            "If you stop receiving these emails, the health alert system may be broken\n"
            "and should be investigated.\n\n"
            f"Sent: {now}"
        ),
    )
    return {"status": "ok"}

"""
S3 PutObject → ECS RunTask: automated recording ingest trigger.

Triggered when a new .mp3 lands in recordings-bucket-1.
Fires the existing ECS Fargate task with RECORDING_S3_KEY set so spike.py
enters recording_ingest mode: transcribe → newsreel ingest → stamp alerts.
"""

import logging
import os

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

ECS_CLUSTER = os.environ["ECS_CLUSTER"]
TASK_DEFINITION = os.environ["TASK_DEFINITION"]
CONTAINER_NAME = os.environ["CONTAINER_NAME"]
SUBNETS = [s.strip() for s in os.environ["SUBNETS"].split(",") if s.strip()]
SECURITY_GROUP = os.environ["SECURITY_GROUP"]

_ecs = boto3.client("ecs")


def handler(event, context):
    for record in event.get("Records", []):
        s3 = record.get("s3", {})
        bucket = s3.get("bucket", {}).get("name", "")
        key = s3.get("object", {}).get("key", "")

        if not key.lower().endswith(".mp3"):
            log.info("Skipping non-mp3 object: %s", key)
            continue

        log.info("New recording s3://%s/%s — launching ECS task", bucket, key)

        resp = _ecs.run_task(
            cluster=ECS_CLUSTER,
            taskDefinition=TASK_DEFINITION,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": SUBNETS,
                    "securityGroups": [SECURITY_GROUP],
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [{
                    "name": CONTAINER_NAME,
                    "environment": [
                        {"name": "RECORDING_S3_KEY", "value": key},
                    ],
                }]
            },
        )

        tasks = resp.get("tasks", [])
        failures = resp.get("failures", [])
        if tasks:
            log.info("ECS task started: %s", tasks[0].get("taskArn", "?"))
        if failures:
            log.error("ECS RunTask failures: %s", failures)
            raise RuntimeError(f"ECS RunTask failed: {failures}")

    return {"statusCode": 200}

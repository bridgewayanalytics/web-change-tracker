"""
Load settings from AWS SSM Parameter Store in AWS/prod mode.
Only fetches parameters not already set in env. Never logs secrets.
"""

import logging
import os

log = logging.getLogger(__name__)

_DEFAULT_API_KEY_PARAM = "/web-change-tracker/prod/openai_api_key"
_DEFAULT_MODEL_PARAM = "/web-change-tracker/prod/openai_model"
_DEFAULT_REASONING_EFFORT_PARAM = "/web-change-tracker/prod/openai_reasoning_effort"


def _should_fetch_from_ssm() -> bool:
    """True when we should attempt to load OpenAI settings from SSM."""
    state_backend = (os.environ.get("STATE_BACKEND") or "").strip().lower()
    environment = (os.environ.get("ENVIRONMENT") or "").strip().lower()
    fetch_flag = (os.environ.get("OPENAI_FETCH_FROM_SSM") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    # AWS/prod: STATE_BACKEND=aws or dynamodb, or ENVIRONMENT=prod
    if state_backend in ("aws", "dynamodb"):
        return True
    if environment in ("prod", "production"):
        return True
    # Local: only if explicitly requested
    if fetch_flag:
        return True
    return False


def load_openai_env_from_ssm() -> None:
    """
    Load OPENAI_API_KEY, OPENAI_MODEL, OPENAI_REASONING_EFFORT from SSM if not set.
    Only runs when STATE_BACKEND=aws|dynamodb, ENVIRONMENT=prod, or OPENAI_FETCH_FROM_SSM=true.
    On failure, logs warning and continues without AI. Never logs the API key.
    """
    if not _should_fetch_from_ssm():
        return

    api_key_param = (
        os.environ.get("OPENAI_API_KEY_SSM_PARAM") or _DEFAULT_API_KEY_PARAM
    ).strip()
    model_param = (
        os.environ.get("OPENAI_MODEL_SSM_PARAM") or _DEFAULT_MODEL_PARAM
    ).strip()
    effort_param = (
        os.environ.get("OPENAI_REASONING_EFFORT_SSM_PARAM")
        or _DEFAULT_REASONING_EFFORT_PARAM
    ).strip()

    params_to_fetch: list[tuple[str, str, bool]] = [
        ("OPENAI_API_KEY", api_key_param, True),
        ("OPENAI_MODEL", model_param, False),
        ("OPENAI_REASONING_EFFORT", effort_param, False),
    ]

    needed = [
        (env_key, param, decrypt)
        for env_key, param, decrypt in params_to_fetch
        if not (os.environ.get(env_key) or "").strip()
    ]
    if not needed:
        return

    try:
        import boto3
    except ImportError:
        log.warning("boto3 not installed, skipping SSM load for OpenAI settings")
        return

    region = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip()
    if not region:
        region = "us-east-1"

    try:
        client = boto3.client("ssm", region_name=region)
    except Exception as e:
        log.warning("Could not create SSM client, skipping OpenAI SSM load: %s", e)
        return

    for env_key, param_name, with_decrypt in needed:
        try:
            kwargs: dict = {"Name": param_name}
            if with_decrypt:
                kwargs["WithDecryption"] = True
            resp = client.get_parameter(**kwargs)
            value = (resp.get("Parameter") or {}).get("Value") or ""
            if value and isinstance(value, str):
                os.environ[env_key] = value.strip()
                if env_key != "OPENAI_API_KEY":
                    log.debug("Loaded %s from SSM parameter %s", env_key, param_name)
        except client.exceptions.ParameterNotFound:
            log.debug("SSM parameter %s not found, skipping %s", param_name, env_key)
        except Exception as e:
            log.warning(
                "Failed to load %s from SSM (%s), continuing without: %s",
                env_key,
                param_name,
                e,
            )


# ---------------------------------------------------------------------------
# Database credentials
# ---------------------------------------------------------------------------

_DB_SSM_PARAMS = [
    # (env_var, ssm_param_path, is_secret)
    ("DATABASE_IP",                "/web-change-tracker/prod/database_ip",                False),
    ("DATABASE_NAME",              "/web-change-tracker/prod/database_name",              False),
    ("DATABASE_PORT",              "/web-change-tracker/prod/database_port",              False),
    ("DATABASE_USERNAME_CHATKIT",  "/web-change-tracker/prod/database_username_chatkit",  False),
    ("DATABASE_PASSWORD_CHATKIT",  "/web-change-tracker/prod/database_password_chatkit",  True),
]


def load_db_env_from_ssm() -> None:
    """
    Load pgvector DB connection settings from SSM if not already set in env.
    Only runs when STATE_BACKEND=aws|dynamodb, ENVIRONMENT=prod, or OPENAI_FETCH_FROM_SSM=true.
    On failure, logs warning and continues (pgvector tools will be skipped).
    """
    if not _should_fetch_from_ssm():
        return

    needed = [
        (env_key, param, decrypt)
        for env_key, param, decrypt in _DB_SSM_PARAMS
        if not (os.environ.get(env_key) or "").strip()
    ]
    if not needed:
        return

    try:
        import boto3
    except ImportError:
        log.warning("boto3 not installed, skipping SSM load for DB settings")
        return

    region = (os.environ.get("AWS_REGION") or "us-east-1").strip()
    try:
        client = boto3.client("ssm", region_name=region)
    except Exception as e:
        log.warning("Could not create SSM client for DB settings: %s", e)
        return

    for env_key, param_name, with_decrypt in needed:
        try:
            kwargs: dict = {"Name": param_name}
            if with_decrypt:
                kwargs["WithDecryption"] = True
            resp = client.get_parameter(**kwargs)
            value = (resp.get("Parameter") or {}).get("Value") or ""
            if value and isinstance(value, str):
                os.environ[env_key] = value.strip()
                log.debug("Loaded %s from SSM parameter %s", env_key, param_name)
        except client.exceptions.ParameterNotFound:
            log.debug("SSM parameter %s not found, skipping %s", param_name, env_key)
        except Exception as e:
            log.warning(
                "Failed to load %s from SSM (%s), continuing without: %s",
                env_key, param_name, e,
            )

"""Post AWS Config NON_COMPLIANT results to a Slack incoming webhook.

EventBridge invokes this function with a "Config Rules Compliance Change"
event. The function formats a plain-text message (no emojis, no attachments)
and posts it to the webhook URL stored in Secrets Manager.

Environment variables:
  WEBHOOK_SECRET_ARN  ARN or name of the secret that holds the webhook URL.
                      The secret value is either the URL itself or JSON with
                      a "webhook_url" key.
  HTTP_TIMEOUT        Seconds to wait for Slack (default 10).

format_message() has no AWS dependencies so it can be unit tested offline.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "10"))
SECRET_JSON_KEY = "webhook_url"
MAX_ANNOTATION_CHARS = 500

# Console host per partition. Regions not listed use the commercial host.
CONSOLE_HOSTS = {
    "cn-": "console.amazonaws.cn",
    "us-gov-": "console.amazonaws-us-gov.com",
}
DEFAULT_CONSOLE_HOST = "console.aws.amazon.com"

_webhook_url_cache: str | None = None


# ---------------------------------------------------------------------------
# Formatting (pure functions)
# ---------------------------------------------------------------------------
def slack_escape(text: str) -> str:
    """Escape the three characters Slack treats as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def console_host(region: str) -> str:
    for prefix, host in CONSOLE_HOSTS.items():
        if region.startswith(prefix):
            return host
    return DEFAULT_CONSOLE_HOST


def rule_console_url(region: str, rule_name: str) -> str:
    """Link to the Config rule's page, which lists its non-compliant resources."""
    host = console_host(region)
    query = urllib.parse.urlencode({"region": region})
    rule = urllib.parse.quote(rule_name, safe="")
    return f"https://{region}.{host}/config/home?{query}#/rules/details?configRuleName={rule}"


def format_message(event: dict) -> dict | None:
    """Build the Slack payload for one compliance change event.

    Returns None when the event is not a NON_COMPLIANT result, so the caller
    can skip it. EventBridge already filters on NON_COMPLIANT, but a direct
    test invocation might not.
    """
    detail = event.get("detail") or {}
    new_result = detail.get("newEvaluationResult") or {}
    if new_result.get("complianceType") != "NON_COMPLIANT":
        return None

    old_result = detail.get("oldEvaluationResult") or {}
    region = detail.get("awsRegion") or event.get("region") or "unknown"
    account = detail.get("awsAccountId") or event.get("account") or "unknown"
    rule = detail.get("configRuleName") or "unknown"
    resource_id = detail.get("resourceId") or "unknown"
    resource_type = detail.get("resourceType") or "unknown"
    previous = old_result.get("complianceType") or "none"
    annotation = (new_result.get("annotation") or "").strip()
    if len(annotation) > MAX_ANNOTATION_CHARS:
        annotation = annotation[: MAX_ANNOTATION_CHARS - 3] + "..."

    lines = [
        "AWS Config: resource is NON_COMPLIANT",
        f"Resource: {resource_id}",
        f"Type: {resource_type}",
        f"Account: {account}",
        f"Region: {region}",
        f"Rule: {rule}",
        f"Previous state: {previous}",
    ]
    if annotation:
        lines.append(f"Detail: {annotation}")
    lines.append(f"Console: {rule_console_url(region, rule)}")

    text = "\n".join(slack_escape(line) for line in lines)
    # mrkdwn false keeps resource names with underscores or asterisks literal.
    return {"text": text, "mrkdwn": False}


# ---------------------------------------------------------------------------
# AWS and HTTP
# ---------------------------------------------------------------------------
def parse_secret(secret_string: str) -> str:
    value = secret_string.strip()
    if value.startswith("{"):
        value = json.loads(value)[SECRET_JSON_KEY]
    if not value.startswith("https://"):
        raise ValueError("Webhook secret does not contain an https URL")
    return value


def get_webhook_url() -> str:
    """Read the webhook URL once per execution environment."""
    global _webhook_url_cache
    if _webhook_url_cache is None:
        import boto3  # imported here so format_message tests need no AWS SDK

        secret_id = os.environ["WEBHOOK_SECRET_ARN"]
        resp = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)
        _webhook_url_cache = parse_secret(resp["SecretString"])
    return _webhook_url_cache


def post_to_slack(url: str, payload: dict) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status != 200:
            raise RuntimeError(f"Slack returned HTTP {resp.status}: {body}")


def handler(event, context):
    payload = format_message(event)
    detail = event.get("detail") or {}
    if payload is None:
        logger.info("Skipping event that is not NON_COMPLIANT: %s", event.get("id"))
        return {"posted": False}

    try:
        post_to_slack(get_webhook_url(), payload)
    except urllib.error.HTTPError as exc:
        # EventBridge invokes Lambda asynchronously. Raising lets Lambda retry
        # the event and then send it to the function's dead-letter queue.
        logger.error("Slack rejected the message: HTTP %s %s", exc.code, exc.reason)
        raise
    logger.info(
        "Posted %s %s for rule %s",
        detail.get("resourceType"),
        detail.get("resourceId"),
        detail.get("configRuleName"),
    )
    return {"posted": True}

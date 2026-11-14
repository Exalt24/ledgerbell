"""Alerts through Amazon SES (the SES v2 API, `SendEmail`).

SES v2 documentation: `POST /v2/email/outbound-emails` with FromEmailAddress, Destination.ToAddresses and Content.Simple.Subject / Body.Text,
signed with AWS Signature Version 4 for the service `ses`. boto3 does the signing and reads the AWS credentials the usual way (environment
variables, the shared credentials file, an instance role), so no key is ever read from the config file or put into a message or a problem string.

A new SES account is in the sandbox: it can send only to addresses that were verified, from an address that was verified, at a low rate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .config import AlertSettings

SUBJECT_MAX = 200


class SesError(RuntimeError):
    pass


def _client(settings: AlertSettings) -> Any:
    try:
        import boto3  # noqa: PLC0415 - optional dependency, only needed when the SES alert is switched on
    except ImportError as exc:
        raise SesError('the SES alert needs boto3: pip install -e ".[aws]"') from exc
    # A fresh session each time, so rotated keys are picked up and one process never holds on to a stale credential.
    return boto3.session.Session().client("sesv2", region_name=settings.ses_region or None, endpoint_url=settings.ses_endpoint_url or None)


def one_line(subject: str) -> str:
    """A subject is one line: a line break inside it would be a header injection in a plain SMTP message and is refused by SES."""
    return " ".join(subject.split())[:SUBJECT_MAX]


def send(settings: AlertSettings, subject: str, body: str, client_factory: Callable[[AlertSettings], Any] = _client) -> str:
    """Sends one alert and returns SES's MessageId. Raises SesError with SES's own error code and message."""
    if not settings.ses_to or not settings.ses_from:
        raise SesError("ses_to and ses_from are both needed")
    client = client_factory(settings)
    try:
        resp = client.send_email(
            FromEmailAddress=settings.ses_from,
            Destination={"ToAddresses": [settings.ses_to]},
            Content={"Simple": {"Subject": {"Data": one_line(subject), "Charset": "UTF-8"}, "Body": {"Text": {"Data": body, "Charset": "UTF-8"}}}},
        )
    except Exception as exc:  # noqa: BLE001 - botocore raises ClientError, EndpointConnectionError and more; each is named, none carries a key
        err = getattr(exc, "response", {}).get("Error", {})
        if err:
            raise SesError(f"{err.get('Code', 'Error')}: {str(err.get('Message', ''))[:200]}") from exc
        raise SesError(type(exc).__name__) from exc
    return str(resp["MessageId"])

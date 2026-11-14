"""A real send through Amazon SES, with a record of what happened that holds no secret.

    python scripts/ses_check.py --credentials-file <aws shared credentials file> --region ap-southeast-2 --to <address> --sender <address> [--create-identity] [--out docs/ses-run-captured.txt]

What it does, in order: names the caller (the IAM user name only, never the key), reads the SES account state (sandbox or production, the daily quota), looks up the
sender and recipient identities, sends ONE alert through `ledgerbell.ses.send` (the function the Notifier calls) when both are verified, and writes the transcript.
boto3 reads the credentials file itself; this script never opens it, never prints a key, and refuses to write a transcript that contains the access key id.
`--create-identity` asks SES to send the verification email for an address that is not yet an identity (the owner then opens the link in that email).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from ledgerbell import ses  # noqa: E402
from ledgerbell.config import AlertSettings  # noqa: E402


def identity_state(client, address: str) -> str:
    try:
        r = client.get_email_identity(EmailIdentity=address)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NotFoundException":
            return "not an identity"
        raise
    return "verified" if r.get("VerifiedForSendingStatus") else "pending verification"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--credentials-file", required=True)
    ap.add_argument("--profile", default="default")
    ap.add_argument("--region", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--sender", required=True)
    ap.add_argument("--create-identity", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()

    os.environ["AWS_SHARED_CREDENTIALS_FILE"] = a.credentials_file
    session = boto3.session.Session(profile_name=a.profile, region_name=a.region)
    key_id = session.get_credentials().access_key
    lines: list[str] = [f"ledgerbell SES check, {datetime.now(UTC).isoformat(timespec='seconds')}", f"region: {a.region}"]
    code = 0

    who = session.client("sts").get_caller_identity()["Arn"]
    lines.append(f"caller: IAM {who.split(':')[-1]}")
    client = session.client("sesv2")
    acct = client.get_account()
    quota = acct.get("SendQuota", {})
    lines.append(
        f"account: sending enabled {acct.get('SendingEnabled')}, production access {acct.get('ProductionAccessEnabled')}"
        f" (sandbox {not acct.get('ProductionAccessEnabled')}), quota {quota.get('Max24HourSend')} per 24 hours, {quota.get('MaxSendRate')} per second"
    )
    states = {}
    for address in dict.fromkeys([a.sender, a.to]):
        states[address] = identity_state(client, address)
        if states[address] == "not an identity" and a.create_identity:
            client.create_email_identity(EmailIdentity=address)
            states[address] = "verification email sent, waiting for the link to be opened"
        lines.append(f"identity {address}: {states[address]}")
    if all(v == "verified" for v in states.values()):
        settings = AlertSettings(ses_to=a.to, ses_from=a.sender, ses_region=a.region)
        message_id = ses.send(settings, "ledgerbell alert test", "This is a test alert from ledgerbell, sent through Amazon SES by scripts/ses_check.py.")
        lines.append(f"sent: one alert, SES MessageId {message_id}")
    else:
        lines.append("not sent: both addresses must be verified identities while the account is in the sandbox")
        code = 1
    text = "\n".join(lines) + "\n"
    if key_id in text:
        print("refusing to write: the transcript contains the access key id", file=sys.stderr)
        return 3
    print(text, end="")
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

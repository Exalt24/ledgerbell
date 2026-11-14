"""The ledgerbell API on Modal: the FastAPI app from src/ledgerbell/api.py served as a serverless web endpoint.

    pip install modal
    modal token new                                   (once; opens the browser)
    modal secret create ledgerbell-api LEDGERBELL_API_KEY=<a long random string> LEDGERBELL_SES_TO=... LEDGERBELL_SES_FROM=... \
        LEDGERBELL_SES_REGION=ap-southeast-2 AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
    modal deploy serverless/modal_app.py              prints the https address of the endpoint

Every setting comes from the Modal Secret named `ledgerbell-api`, so nothing secret is in the image or in this file. The app scales to zero when idle, so the
first call after a quiet spell pays a cold start (measured in the README).
"""

import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi>=0.115", "boto3>=1.35", "requests>=2.32", "tzdata")
    .add_local_python_source("ledgerbell")
)
app = modal.App("ledgerbell-api", image=image)


@app.function(secrets=[modal.Secret.from_name("ledgerbell-api")], max_containers=2, timeout=60)
@modal.asgi_app()
def web():
    from ledgerbell.api import app_from_env

    return app_from_env()

import asyncio
import base64
import hashlib
import tarfile
from io import BytesIO

import httpx
from fastapi.testclient import TestClient
from mpp import Receipt

from app.dynamo import DynamoStore
from app.main import create_app
from tests.test_api import Detector, config, png


def test_lambda_function_url_binary_upload_and_warm_model(dynamodb, monkeypatch):
    from app import lambda_handler

    monkeypatch.setenv("PAYMENT_MODE", "tempo")
    monkeypatch.setenv("TEMPO_CHAIN_ID", "42431")
    monkeypatch.setenv("TEMPO_RECIPIENT", "0x" + "12" * 20)
    monkeypatch.setenv("MPP_SECRET_KEY", "s" * 64)
    monkeypatch.setenv("DYNAMODB_TABLE", dynamodb)
    lambda_handler.adapter.cache_clear()
    lambda_handler.detector.cache_clear()
    event = {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": "/v1/moderate",
        "rawQueryString": "",
        "headers": {
            "content-type": "image/png",
            "host": "example.lambda-url.us-east-1.on.aws",
        },
        "requestContext": {
            "http": {
                "method": "POST",
                "path": "/v1/moderate",
                "sourceIp": "127.0.0.1",
                "protocol": "HTTP/1.1",
            },
            "requestId": "test",
            "stage": "$default",
        },
        "body": base64.b64encode(png()).decode(),
        "isBase64Encoded": True,
    }
    try:
        for _ in range(2):
            response = lambda_handler.handler(event, None)
            assert response["statusCode"] == 402
            assert "www-authenticate" in response["headers"]
        assert lambda_handler.detector.cache_info().misses == 1
        assert lambda_handler.detector.cache_info().hits == 1
    finally:
        lambda_handler.adapter.cache_clear()
        lambda_handler.detector.cache_clear()


def test_duplicate_credentials_across_containers_settle_once(tmp_path, dynamodb):
    async def check():
        settings = config(tmp_path, "tempo", dynamodb_table=dynamodb)
        first = create_app(settings, Detector)
        second = create_app(settings, Detector)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        class Payments:
            async def charge(self, **kwargs):
                calls.append(kwargs)
                entered.set()
                await release.wait()
                return None, Receipt.success("0x" + "ab" * 32)

        async with (
            first.router.lifespan_context(first),
            second.router.lifespan_context(second),
        ):
            first.state.payments = second.state.payments = Payments()
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(first), base_url="http://one"
                ) as a,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(second), base_url="http://two"
                ) as b,
            ):
                headers = {"Authorization": "test-same-credential"}
                task = asyncio.create_task(
                    a.post("/v1/moderate", content=png(), headers=headers)
                )
                await asyncio.wait_for(entered.wait(), 5)
                duplicate = await b.post("/v1/moderate", content=png(), headers=headers)
                assert duplicate.status_code == 503
                assert len(calls) == 1
                release.set()
                paid = await task
                assert paid.status_code == 200
                retry = await b.post("/v1/moderate", content=png(), headers=headers)
                assert retry.json() == paid.json()
                assert len(calls) == 1

    asyncio.run(check())


def test_uncertain_settlement_stays_reserved(tmp_path, dynamodb):
    app = create_app(config(tmp_path, "tempo", dynamodb_table=dynamodb), Detector)

    class UnavailablePayments:
        async def charge(self, **kwargs):
            raise httpx.ReadTimeout("simulated unknown RPC outcome")

    with TestClient(app) as client:
        app.state.payments = UnavailablePayments()
        headers = {"Authorization": "uncertain-proof"}
        assert (
            client.post("/v1/moderate", content=png(), headers=headers).status_code
            == 503
        )
        retry = client.post("/v1/moderate", content=png(), headers=headers)
        assert retry.json()["error"] == "payment_in_progress_or_unconfirmed"
        key = "response:" + hashlib.sha256(b"uncertain-proof").hexdigest()
        assert asyncio.run(DynamoStore(dynamodb).get(key)) is not None


def test_source_offer_contains_build_files_and_no_runtime_secrets(tmp_path):
    with TestClient(create_app(config(tmp_path), Detector)) as client:
        response = client.get("/source")
        assert response.status_code == 200
        with tarfile.open(fileobj=BytesIO(response.content)) as source:
            names = source.getnames()
            assert "nude-api/LICENSE" in names
            assert "nude-api/deploy/Dockerfile.lambda" in names
            assert "nude-api/app/lambda_handler.py" in names
            assert "nude-api/template.yaml" in names
            assert all(
                "/.env" not in name or name.endswith(".env.example") for name in names
            )
            assert all(
                "/data/" not in name and "__pycache__" not in name for name in names
            )

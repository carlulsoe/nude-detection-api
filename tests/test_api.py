import asyncio
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from mpp import Challenge, Credential
from mpp.methods.tempo._attribution import encode
from mpp.methods.tempo.intents import TRANSFER_WITH_MEMO_TOPIC
from PIL import Image

from app.config import Settings
from app.main import create_app
from app.store import SQLiteStore


def png(color="white", size=(32, 32)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class Detector:
    def __init__(self):
        self.calls = 0

    def detect(self, pixels):
        self.calls += 1
        return [{"class": "FEMALE_BREAST_EXPOSED", "score": 0.9, "box": [1, 2, 3, 4]}]


def config(tmp_path, mode="development", **kwargs):
    return Settings(
        _env_file=None,
        payment_mode=mode,
        database_path=str(tmp_path / "service.db"),
        mpp_secret_key="s" * 64,
        tempo_recipient="0x" + "12" * 20,
        **kwargs,
    )


def test_local_api(tmp_path):
    detector = Detector()
    with TestClient(create_app(config(tmp_path), lambda: detector)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/healthz").json()["status"] == "ready"
        response = client.post("/v1/moderate", content=png())
        assert response.status_code == 200
        assert response.json()["nsfw"] is True
        assert response.json()["billing"]["charged"] is False
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("body", "limits", "status"),
    [
        (b"not an image", {}, 422),
        (png(), {"max_image_bytes": 10}, 413),
        (png(), {"max_image_pixels": 100}, 413),
    ],
)
def test_invalid_images_never_reach_payments(tmp_path, body, limits, status):
    detector = Detector()
    with TestClient(
        create_app(config(tmp_path, "tempo", **limits), lambda: detector)
    ) as client:
        response = client.post("/v1/moderate", content=body)
        assert response.status_code == status
        assert detector.calls == 0
        assert "payment-receipt" not in response.headers


def test_real_nudenet_inference(tmp_path):
    with TestClient(create_app(config(tmp_path))) as client:
        result = client.post("/v1/moderate", content=png()).json()
        assert result["model"] == "nudenet-320n"
        assert result["detections"] == []
        assert result["nsfw"] is False


def test_mpp_settlement_retries_body_binding_and_restart(tmp_path, payment_store):
    settings = config(tmp_path, "tempo", dynamodb_table=payment_store)
    detector = Detector()
    app = create_app(settings, lambda: detector)
    image = png()
    tx_hash = "0x" + "ab" * 32
    with TestClient(app) as client:
        unpaid = client.post("/v1/moderate", content=image)
        assert unpaid.status_code == 402
        assert detector.calls == 0
        challenge = Challenge.from_www_authenticate(unpaid.headers["www-authenticate"])
        assert challenge.request["amount"] == "100"
        assert challenge.digest
        authorization = Credential(
            challenge.to_echo(), {"type": "hash", "hash": tx_hash}
        ).to_authorization()
        headers = {"Authorization": authorization}
        rpc_calls = []

        def rpc(request):
            rpc_calls.append(request)
            return httpx.Response(
                200,
                json={
                    "result": {
                        "status": "0x1",
                        "logs": [
                            {
                                "address": settings.tempo_currency,
                                "topics": [
                                    TRANSFER_WITH_MEMO_TOPIC,
                                    "0x" + "00" * 32,
                                    "0x" + "00" * 12 + settings.tempo_recipient[2:],
                                    encode(challenge.id, challenge.realm),
                                ],
                                "data": "0x" + format(100, "064x"),
                            }
                        ],
                    }
                },
            )

        # Replace only the RPC transport; run the real SDK's HMAC, digest,
        # transfer, memo, amount, recipient, and replay verification.
        intent = app.state.payments.method.intents["charge"]
        original_client = intent._http_client
        intent._http_client = httpx.AsyncClient(transport=httpx.MockTransport(rpc))
        changed = client.post("/v1/moderate", content=png("black"), headers=headers)
        assert changed.status_code == 402
        assert not rpc_calls
        paid = client.post("/v1/moderate", content=image, headers=headers)
        assert paid.status_code == 200, paid.text
        assert paid.json()["billing"]["charged"] is True
        assert "payment-receipt" in paid.headers
        retry = client.post("/v1/moderate", content=image, headers=headers)
        assert retry.json() == paid.json()
        assert detector.calls == 1
        assert (
            client.post(
                "/v1/moderate", content=png("black"), headers=headers
            ).status_code
            == 409
        )

        # Re-encoding the same proof cannot bypass the SDK transaction store.
        alternate = Credential(
            challenge.to_echo(),
            {"hash": tx_hash, "type": "hash"},
            source="did:pkh:eip155:42431:0x" + "00" * 20,
        ).to_authorization()
        assert (
            client.post(
                "/v1/moderate", content=image, headers={"Authorization": alternate}
            ).status_code
            == 402
        )
        asyncio.run(intent._http_client.aclose())
        intent._http_client = original_client

    with TestClient(create_app(settings, lambda: detector)) as restarted:
        assert (
            restarted.post("/v1/moderate", content=image, headers=headers).json()
            == paid.json()
        )
        assert detector.calls == 1


def test_atomic_durable_replay_store(tmp_path):
    async def check():
        store = SQLiteStore(str(tmp_path / "replay.db"))
        results = await asyncio.gather(
            *(store.put_if_absent("transaction", i) for i in range(10))
        )
        assert results.count(True) == 1
        restarted = SQLiteStore(str(tmp_path / "replay.db"))
        assert await restarted.put_if_absent("transaction", 99) is False

    asyncio.run(check())


def test_tempo_requires_configuration():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            payment_mode="tempo",
            mpp_secret_key=None,
            tempo_recipient=None,
        )


def test_chain_id_from_environment(monkeypatch):
    monkeypatch.setenv("PAYMENT_MODE", "development")
    monkeypatch.setenv("TEMPO_CHAIN_ID", "42431")
    assert Settings(_env_file=None).tempo_chain_id == 42431
    monkeypatch.setenv("TEMPO_CHAIN_ID", "1")
    with pytest.raises(ValueError):
        Settings(_env_file=None)

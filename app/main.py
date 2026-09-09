import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from mpp import Challenge
from mpp.errors import PaymentError, VerificationError
from mpp.methods.tempo import ChargeIntent, tempo
from mpp.server import Mpp
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.detection import decode_image, detect
from app.source import source_archive
from app.store import SQLiteStore

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, detector_factory=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings()
        app.state.config = config
        if config.dynamodb_table:
            from app.dynamo import DynamoStore

            app.state.store = DynamoStore(config.dynamodb_table)
        else:
            app.state.store = SQLiteStore(config.database_path)
        app.state.slot = asyncio.Lock()
        if detector_factory is None:
            from nudenet import NudeDetector

            app.state.detector = await run_in_threadpool(NudeDetector)
        else:
            app.state.detector = detector_factory()
        async with httpx.AsyncClient(timeout=30) as rpc_client:
            app.state.payments = None
            if config.payment_mode == "tempo":
                intent = ChargeIntent(http_client=rpc_client)
                app.state.payments = Mpp.create(
                    method=tempo(
                        intents={"charge": intent},
                        recipient=config.tempo_recipient,
                        currency=config.tempo_currency,
                        chain_id=config.tempo_chain_id,
                    ),
                    realm="nude-api/v1/moderate",
                    secret_key=config.mpp_secret_key.get_secret_value(),
                    store=app.state.store,
                )
            yield

    app = FastAPI(
        title="Nude API",
        version="0.1.0",
        lifespan=lifespan,
        description="NudeNET image moderation with per-image Tempo MPP payments.",
    )

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(Path(__file__).parent / "index.html")

    @app.get("/source", include_in_schema=False)
    async def source():
        from fastapi.responses import Response

        return Response(
            source_archive(),
            media_type="application/gzip",
            headers={
                "Content-Disposition": 'attachment; filename="nude-api-source.tar.gz"',
            },
        )

    @app.get("/healthz")
    async def health():
        return {"status": "ready", "model": "nudenet-320n"}

    @app.get("/v1/pricing")
    async def pricing():
        config = app.state.config
        return {
            "amount_per_image": str(config.price_per_image),
            "currency": config.tempo_currency,
            "chain_id": config.tempo_chain_id,
            "payment_mode": config.payment_mode,
            "intent": "charge",
            "max_image_bytes": config.max_image_bytes,
            "max_image_pixels": config.max_image_pixels,
        }

    @app.post(
        "/v1/moderate",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    mime: {"schema": {"type": "string", "format": "binary"}}
                    for mime in ("image/jpeg", "image/png", "image/webp")
                },
            },
        },
        responses={
            402: {"description": "MPP payment challenge"},
            413: {"description": "Image too large"},
            503: {"description": "Busy or outcome requires reconciliation"},
        },
    )
    async def moderate(request: Request):
        config = app.state.config
        # One admitted request per process bounds decoding and inference memory.
        # Across Lambda environments, the durable response reservation below
        # coordinates duplicate credentials before any settlement attempt.
        if app.state.slot.locked():
            raise HTTPException(
                503, "Service busy; retry later", headers={"Retry-After": "1"}
            )
        async with app.state.slot:
            body = bytearray()
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > config.max_image_bytes:
                        raise HTTPException(413, "Image exceeds the byte limit")
            body = bytes(body)
            digest = hashlib.sha256(body).hexdigest()
            authorization = request.headers.get("Authorization")
            cache_key = (
                "response:" + hashlib.sha256(authorization.encode()).hexdigest()
                if authorization and app.state.payments
                else None
            )
            if cache_key:
                cached = await app.state.store.get(cache_key)
                if cached:
                    if cached["digest"] != digest:
                        raise HTTPException(
                            409, "This credential belongs to a different image"
                        )
                    return JSONResponse(
                        cached["body"],
                        status_code=cached["status"],
                        headers=cached["headers"],
                    )

            pixels = await run_in_threadpool(
                decode_image, body, config.max_image_pixels
            )
            request_id = str(uuid4())
            reservation = {
                "digest": digest,
                "status": 503,
                "headers": {},
                "body": {
                    "error": "payment_in_progress_or_unconfirmed",
                    "request_id": request_id,
                    "detail": "Retry the identical credential to recover the result. Do not pay again.",
                },
            }
            if cache_key and not await app.state.store.put_if_absent(
                cache_key, reservation
            ):
                cached = await app.state.store.get(cache_key)
                if cached and cached["digest"] != digest:
                    raise HTTPException(
                        409, "This credential belongs to a different image"
                    )
                cached = cached or reservation
                return JSONResponse(
                    cached["body"],
                    status_code=cached["status"],
                    headers=cached["headers"],
                )
            headers = {}
            reference = None
            if app.state.payments:
                try:
                    result = await app.state.payments.charge(
                        authorization=authorization,
                        amount=str(config.price_per_image),
                        body=body,
                        description="One image moderation",
                    )
                except VerificationError:
                    if cache_key:
                        await app.state.store.delete(cache_key)
                    raise
                if isinstance(result, Challenge):
                    if cache_key:
                        await app.state.store.delete(cache_key)
                    return JSONResponse(
                        {"error": "payment_required", "pricing": "/v1/pricing"},
                        status_code=402,
                        headers={
                            "WWW-Authenticate": result.to_www_authenticate(
                                app.state.payments.realm
                            )
                        },
                    )
                _, receipt = result
                reference = receipt.reference
                headers["Payment-Receipt"] = receipt.to_payment_receipt()

            # Record settlement before inference so a failed paid request cannot
            # silently settle again. Persisted failures include the receipt.
            pending = {
                "digest": digest,
                "status": 503,
                "headers": headers,
                "body": {
                    "error": "paid_result_unavailable",
                    "request_id": request_id,
                    "payment_reference": reference,
                    "detail": "Contact the operator with this reference; do not pay again.",
                },
            }
            if cache_key:
                await app.state.store.put(cache_key, pending)
            try:
                output = await run_in_threadpool(
                    detect, app.state.detector, pixels, config.nsfw_threshold
                )
            except Exception:
                logger.exception("Inference failed for request %s", request_id)
                return JSONResponse(pending["body"], status_code=503, headers=headers)
            output.update(
                request_id=request_id,
                billing={
                    "charged": reference is not None,
                    "amount": str(config.price_per_image) if reference else "0",
                    "payment_reference": reference,
                },
            )
            if cache_key:
                await app.state.store.put(
                    cache_key,
                    {
                        "digest": digest,
                        "status": 200,
                        "headers": headers,
                        "body": output,
                    },
                )
            return JSONResponse(output, headers=headers)

    @app.exception_handler(TimeoutError)
    async def upload_timeout(request, exc):
        return JSONResponse({"error": "request_timeout"}, status_code=408)

    @app.exception_handler(PaymentError)
    async def payment_error(request, exc):
        return JSONResponse(
            exc.to_problem_details(),
            status_code=exc.status,
            media_type="application/problem+json",
        )

    @app.exception_handler(VerificationError)
    async def verification_error(request, exc):
        return JSONResponse(
            {"error": "payment_verification_failed", "detail": str(exc)},
            status_code=402,
        )

    @app.exception_handler(httpx.HTTPError)
    async def rpc_error(request, exc):
        return JSONResponse(
            {
                "error": "payment_outcome_unconfirmed",
                "detail": "Payment RPC unavailable. Check the transaction before paying again.",
            },
            status_code=503,
        )

    return app


app = create_app()

"""Lambda Function URL entrypoint; reuse the loaded model on warm invocations."""

from functools import lru_cache

from mangum import Mangum

from app.config import Settings
from app.main import create_app


@lru_cache(maxsize=1)
def detector():
    from nudenet import NudeDetector

    return NudeDetector()


@lru_cache(maxsize=1)
def adapter():
    config = Settings()
    if config.payment_mode != "tempo" or not config.dynamodb_table:
        raise ValueError("Lambda requires Tempo payments and a shared DynamoDB table")
    return Mangum(create_app(config, detector_factory=detector), lifespan="on")


def handler(event, context):
    return adapter()(event, context)

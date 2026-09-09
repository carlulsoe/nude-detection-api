import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def dynamodb(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="payments",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "key", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "key", "KeyType": "HASH"}],
        )
        yield "payments"


@pytest.fixture(params=["sqlite", "dynamodb"])
def payment_store(request):
    return request.getfixturevalue("dynamodb") if request.param == "dynamodb" else None

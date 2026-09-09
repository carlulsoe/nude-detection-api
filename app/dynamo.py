"""Shared, strongly consistent payment store for concurrent Lambda environments."""

import json

import boto3
from botocore.exceptions import ClientError
from starlette.concurrency import run_in_threadpool


class DynamoStore:
    def __init__(self, table_name: str):
        self.client = boto3.client("dynamodb")
        self.table_name = table_name

    async def get(self, key):
        response = await run_in_threadpool(
            self.client.get_item,
            TableName=self.table_name,
            Key={"key": {"S": key}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return json.loads(item["value"]["S"]) if item else None

    async def put(self, key, value):
        await run_in_threadpool(
            self.client.put_item,
            TableName=self.table_name,
            Item={"key": {"S": key}, "value": {"S": json.dumps(value)}},
        )

    async def put_if_absent(self, key, value):
        try:
            await run_in_threadpool(
                self.client.put_item,
                TableName=self.table_name,
                Item={"key": {"S": key}, "value": {"S": json.dumps(value)}},
                ConditionExpression="attribute_not_exists(#key)",
                ExpressionAttributeNames={"#key": "key"},
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    async def delete(self, key):
        await run_in_threadpool(
            self.client.delete_item, TableName=self.table_name, Key={"key": {"S": key}}
        )

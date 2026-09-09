"""Pay for one image. Uses testnet by default; requires a funded wallet."""

import asyncio
import os
import sys
from pathlib import Path

from mpp.client import Client
from mpp.methods.tempo import ChargeIntent, TempoAccount, tempo


async def main():
    image = Path(sys.argv[1]).read_bytes()
    account = TempoAccount.from_env()
    async with Client(
        methods=[
            tempo(
                account=account,
                chain_id=int(os.environ.get("TEMPO_CHAIN_ID", "42431")),
                intents={"charge": ChargeIntent()},
            )
        ]
    ) as client:
        response = await client.post(
            os.environ.get("API_URL", "http://127.0.0.1:8000") + "/v1/moderate",
            content=image,
            headers={"Content-Type": "application/octet-stream"},
        )
        print(response.status_code)
        print(response.text)
        response.raise_for_status()


if __name__ == "__main__":
    asyncio.run(main())

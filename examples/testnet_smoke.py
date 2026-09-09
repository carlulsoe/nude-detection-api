"""One real Moderato payment using an ephemeral, faucet-funded test payer.

Run: uv run python examples/testnet_smoke.py --recipient 0x... --execute
No mainnet support. Wallet key stays in process memory and is never printed.
"""

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path

import httpx
from eth_account import Account
from mpp import Challenge
from mpp.methods.tempo import ChargeIntent, TempoAccount, tempo
from PIL import Image

RPC = "https://rpc.moderato.tempo.xyz"
TOKEN = "0x20c0000000000000000000000000000000000000"


async def main(recipient):
    async with httpx.AsyncClient(timeout=60) as rpc:

        async def call(method, params):
            response = await rpc.post(
                RPC,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                },
            )
            response.raise_for_status()
            result = response.json()
            if "error" in result:
                raise RuntimeError(f"RPC {method} failed: {result['error']}")
            return result["result"]

        async def balance(address):
            result = await call(
                "eth_call",
                [
                    {
                        "to": TOKEN,
                        "data": "0x70a08231" + address[2:].lower().zfill(64),
                    },
                    "latest",
                ],
            )
            return int(result, 16)

        assert int(await call("eth_chainId", []), 16) == 42431
        account = TempoAccount.from_key(Account.create().key.hex())
        print("Ephemeral test payer:", account.address, flush=True)
        await call("tempo_fundAddress", [account.address])
        for _ in range(30):
            if await balance(account.address) >= 100:
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Faucet funding not observed; no payment attempted")
        before = await balance(recipient)

        with tempfile.TemporaryDirectory(prefix="nude-testnet-") as directory:
            env = {
                **os.environ,
                "PAYMENT_MODE": "tempo",
                "TEMPO_RECIPIENT": recipient,
                "TEMPO_CHAIN_ID": "42431",
                "TEMPO_CURRENCY": TOKEN,
                "PRICE_PER_IMAGE": "0.0001",
                "MPP_SECRET_KEY": secrets.token_hex(32),
                "DATABASE_PATH": str(Path(directory) / "payments.sqlite3"),
            }
            env.pop("DYNAMODB_TABLE", None)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "18765",
                "--no-access-log",
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                async with httpx.AsyncClient(
                    base_url="http://127.0.0.1:18765", timeout=60
                ) as client:
                    for _ in range(50):
                        if process.returncode is not None:
                            error = (
                                (await process.stderr.read())
                                .decode()
                                .replace(env["MPP_SECRET_KEY"], "[redacted]")
                            )
                            raise RuntimeError(f"Test server failed to start: {error}")
                        try:
                            response = await client.get("/healthz")
                            if response.status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        await asyncio.sleep(0.2)
                    else:
                        raise RuntimeError("Test server did not become ready")
                    buffer = BytesIO()
                    Image.new("RGB", (640, 480), "white").save(buffer, format="PNG")
                    image = buffer.getvalue()
                    unpaid = await client.post("/v1/moderate", content=image)
                    assert unpaid.status_code == 402
                    challenge = Challenge.from_www_authenticate(
                        unpaid.headers["www-authenticate"]
                    )
                    assert challenge.request["recipient"].lower() == recipient.lower()
                    assert challenge.request["currency"].lower() == TOKEN.lower()
                    assert challenge.request["amount"] == "100"
                    assert challenge.request["methodDetails"]["chainId"] == 42431
                    method = tempo(
                        account=account,
                        chain_id=42431,
                        intents={"charge": ChargeIntent()},
                    )
                    credential = await method.create_credential(challenge)
                    headers = {"Authorization": credential.to_authorization()}
                    paid = await client.post(
                        "/v1/moderate", content=image, headers=headers
                    )
                    # Never create a second credential if this attempt fails.
                    if paid.status_code != 200:
                        raise RuntimeError(
                            f"Paid request returned {paid.status_code}: {paid.text}"
                        )
                    result = paid.json()
                    tx_hash = result["billing"]["payment_reference"]
                    receipt = await call("eth_getTransactionReceipt", [tx_hash])
                    assert receipt["status"] == "0x1"
                    retry = await client.post(
                        "/v1/moderate", content=image, headers=headers
                    )
                    assert retry.json() == result
                    after = await balance(recipient)
                    assert after - before >= 100
                    report = {
                        "chain_id": 42431,
                        "recipient": recipient,
                        "payer": account.address,
                        "token": TOKEN,
                        "amount": "0.0001",
                        "transaction_hash": tx_hash,
                        "recipient_balance_delta_base_units": after - before,
                        "http_status": paid.status_code,
                        "nsfw": result["nsfw"],
                        "identical_retry_recovered": retry.json() == result,
                        "transaction_status": receipt["status"],
                    }
                    Path("data").mkdir(exist_ok=True)
                    Path("data/testnet-report.json").write_text(
                        json.dumps(report, indent=2) + "\n"
                    )
                    print(json.dumps(report, indent=2), flush=True)
            finally:
                if process.returncode is None:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipient", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute one faucet-funded testnet payment",
    )
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required to perform the testnet transaction")
    from eth_utils import to_checksum_address

    asyncio.run(main(to_checksum_address(args.recipient)))

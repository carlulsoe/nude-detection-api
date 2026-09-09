# Nude API

An open-source, serverless SaaS API for NudeNET image moderation, paid per image
using Tempo's Machine Payments Protocol (MPP). AWS Lambda serves the Python model
and HTTPS endpoint; on-demand DynamoDB stores payments and results. Local
development uses SQLite. Includes a landing page, OpenAPI docs, and an AGPL source offer.

## Run locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
uv sync --frozen
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Open http://127.0.0.1:8000 for the landing page or `/docs` for the API reference.
The example environment explicitly disables payments for local development.
Without that setting, the service defaults to requiring Tempo configuration.

```bash
curl http://127.0.0.1:8000/v1/moderate \
  -H 'Content-Type: image/jpeg' \
  --data-binary @image.jpg
```

Send raw image bytes, not multipart or JSON. Accepts JPEG, PNG, and static WebP;
defaults to 10 MiB and 12 million pixels. EXIF orientation is applied and images
are resized to fit 1280 × 1280 before inference. Bounding boxes use `[x, y, width,
height]` in the returned `image.width` / `image.height` coordinate space.

`nsfw` means at least one configured explicit-part detection meets the threshold
(default 0.6). Policy `exposed-parts-v1` flags exposed breasts, genitals, buttocks,
and anus; see `app/detection.py` for exact NudeNET labels. `score` is the highest
confidence among these detections, **not a calibrated whole-image probability**.
No detections means the model found none, not that the image is guaranteed safe.

## Enable Tempo MPP

In `.env`, set:

```dotenv
PAYMENT_MODE=tempo
MPP_SECRET_KEY=<output of openssl rand -hex 32>
TEMPO_RECIPIENT=<your 0x receiving address>
TEMPO_CHAIN_ID=42431
TEMPO_CURRENCY=0x20c0000000000000000000000000000000000000
PRICE_PER_IMAGE=0.0001
```

This configuration uses Tempo testnet (42431). Set the chain to 4217 and explicitly
confirm the accepted token address for mainnet before launch. The SDK is configured
for tokens with six decimals. The server needs a receiving address and persistent
challenge secret; it does not need the recipient's private key. The client pays gas.

An unpaid valid upload returns **402** with an official SDK `WWW-Authenticate:
Payment` challenge. An MPP client pays and resends identical image bytes with an
`Authorization: Payment` credential. Successful verification yields a moderation
result and `Payment-Receipt` header. Invalid images are rejected before settlement.

The runnable client example uses `TEMPO_PRIVATE_KEY` from the caller's environment:

```bash
# This command spends from the configured client wallet. Testnet is the default.
uv run python examples/moderate.py image.jpg
```

`API_URL` defaults to `http://127.0.0.1:8000`. Set `TEMPO_CHAIN_ID` on the client to
match the server. Use a wallet funded on that chain. Review the advertised price at
`/v1/pricing` before running an automatically paying client.

## Payments, persistence, and failure behavior

- SDK challenges bind to the exact image body, amount, recipient, and chain.
- DynamoDB implements the shared atomic replay store for Lambda. Local development
  uses SQLite. Preserve either database across deployments; deleting it removes
  replay protection. DynamoDB reads are strongly consistent.
- Repeating the same authorization and image returns the persisted JSON result and
  receipt. Reusing that authorization for another image returns 409.
- One request is admitted per process. Lambda scales across execution environments;
  conditional DynamoDB writes reserve each credential before payment. Concurrent
  duplicates get 503 until the original JSON response is available. An uncertain
  settlement keeps its reservation until an operator reconciles it; no automatic
  expiry can reopen a possibly paid request.
- Images are processed in memory. The database retains hashed credentials, image
  digests, transaction references, and paid JSON responses, including detections.
  There is no automatic retention cleanup in this version. Protect the database
  and backups. Never log request bodies or authorization headers at the proxy.
- Settlement precedes inference. If inference fails after payment, the recorded
  failure carries a payment reference for operator reconciliation. There is no
  automated refund. Do not describe this as billing only successful inferences.
- A process failure between on-chain settlement and saving the result can leave a
  paid request without a recoverable result. The SDK transaction record blocks
  reusing that transfer; reconcile the transaction manually instead of paying again.

Do not run multiple replicas against separate replay databases. Use DYNAMODB_TABLE
for shared storage and duplicate coordination. Automated settlement reconciliation
and refunds remain unimplemented; unknown outcomes require operator review.

## Deploy serverlessly on AWS

`template.yaml` defines a Lambda container, a public HTTPS Function URL gated by
MPP in the application, an on-demand DynamoDB table, and 14-day CloudWatch logs.
It defaults to your testnet recipient:
`0x9c06F3329dA1f694Cb3f60e0B89e5CE8af5dadCc`.

There are no EC2 instances, VPC/NAT gateway, provisioned concurrency, or API Gateway.
The bundled model is baked into the image and cached across warm invocations.
The initial setting is 2 GiB memory with up to five concurrent environments.
DynamoDB replay records are retained on stack deletion and replacement, with
point-in-time recovery enabled. Reconnect an existing retained table before serving
payments after recreating a stack; a fresh table loses the original replay history.

Requires Docker, AWS CLI credentials, and AWS SAM CLI. These commands create
billable resources and publish a public endpoint in the selected AWS account:

```bash
sam build
sam deploy --guided --resolve-image-repos
```

During guided deployment, select the intended account/region and provide a persistent
random `MppSecret` (generate with `openssl rand -hex 32`). Keep chain ID `42431` for
testnet. This secret signs challenges and is not a wallet key. The CloudFormation
parameter is masked using `NoEcho`; Lambda administrators can read environment
configuration, so treat deployment access as sensitive. Do not commit secret values
or a generated `samconfig.toml` containing them.

The stack outputs `ApiUrl`. `/docs`, `/v1/pricing`, `/v1/moderate`, and `/source`
are available on it. No domain is required for testing. The Lambda handler refuses
to run with disabled payments or without DynamoDB.

Uploads are limited to **4 MiB** on Lambda to leave room for base64 encoding and
headers in its 6 MiB buffered request limit. Larger requests can be rejected by
Lambda before the app receives them. Local Docker defaults to 10 MiB.

Compute scales to zero, but ECR images, DynamoDB storage/backups, logs, and requests
still have costs. A cold start loads Python dependencies and the model. Unpaid 402
requests also incur compute and decoding costs. Reserved concurrency limits simultaneous
work, not total spending; establish budget alerts and measure real traffic before
opening a mainnet service. Free-tier eligibility does not guarantee a $0 bill.

## Local Docker

```bash
docker compose up --build -d
```

The service listens on loopback port 8000 and persists SQLite in a named volume.
For public hosting, configure Tempo payments and place it behind a TLS reverse proxy
with upload, connection, and rate limits. Model loading completes before readiness.
The container runs as a non-root user. No hosting account or public deployment is
configured by this repository.

## Validate

```bash
uv run --extra aws ruff check app tests examples
uv run --extra aws ruff format --check app tests examples
uv run --extra aws pytest -q
```

Tests include a real bundled NudeNET inference on a generated blank image and the
actual MPP SDK verification flow against simulated JSON-RPC transfer receipts.
They cover request binding, settlement, repeat requests, durable replay rejection,
restart recovery, input rejection, cross-container duplicate coordination using
Moto's DynamoDB emulation, and real Mangum Lambda-event decoding with a warm model.
They do **not** prove deployed AWS behavior or moderation accuracy on customer images.

For an explicit real testnet integration run:

```bash
uv run python examples/testnet_smoke.py \
  --recipient 0x9c06F3329dA1f694Cb3f60e0B89e5CE8af5dadCc --execute
```

This generates an ephemeral payer key in memory, requests faucet funds, starts a
local HTTP service, and pays exactly one image charge on Moderato. It checks the
on-chain transaction receipt, recipient balance increase, real NudeNET response,
and identical-credential retry recovery. It cannot target mainnet. A public-only
report is written to `data/testnet-report.json`; wallet keys are never printed or
saved. If a submitted payment has an uncertain outcome, investigate its transaction
before launching another run.

## Product scope and economics

NudeNET detects body parts; it is not a drop-in replacement for Google Vision
SafeSearch. It does not assess violence, spoofing, medical context, age, or legality.
Before migrating customers, evaluate precision and recall on a representative,
appropriately obtained labeled image set and tune the explicit-part policy.

The default **0.0001 token/image** is a configurable starting assumption, not a
validated business price. At a hypothetical $1 token value, that is $0.10 per
1,000 images and $100 revenue per million images, before gas arrangements and costs.

Open-source inference removes a vendor's per-image bill; it does not make hosting,
CPU, bandwidth, storage, monitoring, or support free. The quoted $2,500/month saving
cannot be established without the original volume and bill. Estimate margin with:

```text
monthly revenue = paid images × price per image
monthly margin = revenue − compute − bandwidth − storage − payment costs − operations
```

Benchmark latency, memory, and sustained images/second on the intended host before
setting capacity or promising savings. A launch also needs real testnet payment
verification on the deployed endpoint, failure reconciliation, and abuse controls.

## Upstream references

- [NudeNET model and labels](https://github.com/notAI-tech/NudeNet/tree/v3)
- [NudeNET upstream AGPL-3.0 license](https://github.com/notAI-tech/NudeNet/blob/v3/LICENSE)
- [Official Python MPP SDK](https://github.com/tempoxyz/pympp)
- [MPP Python documentation](https://mpp.dev/sdk/python)

This service is licensed **AGPL-3.0-only** (see LICENSE and NOTICE). An unpaid
`/source` endpoint and footer link offer an archive of deployed application source,
lockfile, tests, and build/deployment files. The archive uses an explicit file list
and code-directory filters; it excludes runtime data, wallet keys, `.env`, caches,
and credentials. Both container builds include the source-offer files.

Dependencies retain their own licensing. NOTICE links NudeNET's upstream source and
bundled model provenance. The service license does not grant additional rights to
third-party weights. If distributing built containers, also fulfill corresponding
source obligations for bundled copyleft dependencies; the service source archive
alone is not a complete third-party source distribution.

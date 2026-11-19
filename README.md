# LLM-Subtrans FastAPI Demo

A local REST wrapper around the **LLM-Subtrans** CLI. Intended for local integration testing from a NestJS backend.

## What it does

- `POST /v1/transcriptions`: upload media and generate SRT/VTT/ASS using LLM-Subtrans' transcription CLI.
- `POST /v1/translations`: upload SRT/VTT/ASS and translate it with the Gemini provider.
- `POST /v1/process`: transcribe a media file, then translate the resulting subtitle into one or more target languages.
- `GET /v1/jobs/{jobId}`: inspect job status.
- `GET /v1/jobs/{jobId}/files/{filename}`: download generated subtitle.
- `GET /health`: health check.

The wrapper calls the documented LLM-Subtrans command-line tools using `subprocess` with argument arrays (not shell strings).

## Important scope

LLM-Subtrans currently has a separate transcription workflow and supports cloud transcription providers, including Gemini (`gemini-3.5-transcribe` according to its README). Subtitle translation uses its Gemini provider (`gemini-subtrans`). Transcription requires `ffmpeg` and `ffprobe`.

This demo is intentionally simple:
- Job state is stored in process memory. Restarting the API loses job status. Do not use this job store in production.
- It does not include Redis, durable queueing, authentication unless `SERVICE_API_KEY` is configured, user quotas, R2 upload, or cleanup scheduling.
- `--align` and `--diarize` are passed to the transcription CLI; availability/quality depends on the selected provider/model.
- Confirm model IDs and provider names against the installed LLM-Subtrans version. Its CLI supports `scripts/transcribe.py --list-providers`.
- This project is a wrapper, not a fork or copy of LLM-Subtrans.

## Requirements

- Ubuntu 22.04/24.04 (or another Linux with Python 3.10+)
- Python 3.10+
- FFmpeg and ffprobe
- Git
- Gemini gateway API key

## Install

```bash
unzip llm-subtrans-fastapi-demo.zip
cd llm-subtrans-fastapi-demo

chmod +x scripts/*.sh
./scripts/setup_local.sh
```

Edit `.env` and set:

```env
GEMINI_API_KEY=YOUR_GATEWAY_API_KEY
GEMINI_BASE_URL=https://ai-gateway.lutech.vn/llm/gemini
```

Start the server:

```bash
./scripts/run_local.sh
```

Open Swagger UI: http://127.0.0.1:8100/docs

Health check:

```bash
curl http://127.0.0.1:8100/health
```

## Test 1: Transcribe video/audio to subtitle

```bash
curl -X POST http://127.0.0.1:8100/v1/transcriptions \
  -F "file=@./sample.mp4" \
  -F "sourceLanguage=English" \
  -F "format=srt"
```

Response:

```json
{"jobId":"...","status":"queued"}
```

Poll:

```bash
curl http://127.0.0.1:8100/v1/jobs/JOB_ID
```

When completed, download using `result.downloadPath` from the job response, e.g. `/v1/jobs/{jobId}/files/original.srt`.

## Test 2: Translate an existing SRT

```bash
curl -X POST http://127.0.0.1:8100/v1/translations \
  -F "file=@./input.srt" \
  -F "targetLanguage=French"
```

Poll the returned job ID. On completion, `result.downloadPath` is the relative download endpoint, e.g. `/v1/jobs/{jobId}/files/translated-french.srt`.

## Test 3: Full pipeline: transcribe + translate to several languages

```bash
curl -X POST http://127.0.0.1:8100/v1/process \
  -F "file=@./sample.mp4" \
  -F "sourceLanguage=English" \
  -F "targetLanguages=French,German,Spanish,Italian,Dutch,Portuguese" \
  -F "format=srt"
```

Poll the job:

```bash
curl http://127.0.0.1:8100/v1/jobs/JOB_ID
```

On completion, the result includes the original subtitle and a download path for each translation.

## NestJS call example

```ts
import axios from 'axios';
import FormData from 'form-data';
import fs from 'node:fs';

const form = new FormData();
form.append('file', fs.createReadStream('/tmp/video.mp4'));
form.append('sourceLanguage', 'English');
form.append('targetLanguages', 'French,German,Spanish');
form.append('format', 'srt');

const { data } = await axios.post(
  'http://127.0.0.1:8100/v1/process',
  form,
  { headers: form.getHeaders(), maxBodyLength: Infinity }
);

const jobId = data.jobId;
// Poll GET /v1/jobs/{jobId}; in production, prefer webhook/SSE or a queue event.
```

If NestJS and this API run in separate Docker containers, do not use `127.0.0.1`; use the Docker service name, e.g. `http://subtitle-service:8100`.

## Troubleshooting

### Incoming request logs

Each incoming HTTP request writes a `bridge_http_request` INFO log with its
method, path and optional `X-Correlation-ID`. This confirms that the request
reached the Python API; it does not identify which service sent it. Query
strings, headers and request bodies are excluded. Job status polling also logs
each request, so frequent polling increases log volume.

When running under PM2, use `pm2 logs <app-name> --lines 100` and look for
`bridge_http_request`. Send an `X-Correlation-ID` from NestJS to match its
request with the Python log. Uvicorn must be configured to emit INFO logs.

### Token usage and estimated cost logs

Each CLI invocation writes an `llm_usage` JSON log through Uvicorn, including
`job_id`, `operation`, `model`, elapsed seconds, return code, input/output/total
tokens, and USD costs. Summaries are also logged on failure or timeout when the
engine has emitted usage. Subtitle content and prompts are not included in usage logs.
Each observed Gemini response also writes a `gemini_request_usage` JSON log
with its token counts and estimated USD cost. These logs are emitted when the
job subprocess exits and can be matched to the summary using `job_id`.
The same records are also appended as JSON Lines to
`DATA_DIR/logs/gemini-usage.jsonl` (by default `data/logs/gemini-usage.jsonl`),
with a UTC `timestamp` on each line. The file rotates at 10 MiB and retains up
to five older files; older records are eventually deleted. For example, run
`tail -f data/logs/gemini-usage.jsonl` to inspect new records. File logging is
designed for the project's single Uvicorn worker and does not replace billing records.

Set `TRANSLATION_INPUT_USD_PER_MILLION` and
`TRANSLATION_OUTPUT_USD_PER_MILLION` in `.env` to the prices for your configured
model and billing tier. The equivalent `TRANSCRIPTION_*` options apply if the
engine emits transcription token summaries. No default prices are assumed.

`estimated_cost_usd = (input_tokens * input_rate + output_tokens * output_rate) / 1,000,000`.
`engine_reported_cost_usd` is the CLI-reported cost, kept separately from this
estimate. Missing usage or missing/invalid rates produce `null`, not zero.
For Gemini transcription, a local CLI launcher observes the existing
`_create_interaction` response and emits numeric `response.usage` metadata.
The wrapper sums all observed chunks and includes thinking tokens in output
costs (`usage_source: "gemini_response"`). No additional provider call is made,
and the upstream checkout is not modified. Missing provider metadata remains
unknown; partial usage is marked `usage_complete: false` and has no cost estimate.
This adapter relies on the upstream Gemini client's `_create_interaction` method;
if unavailable after an engine upgrade, the original CLI still runs and logs a warning.
Translation observes responses processed by the upstream Gemini client, including
responses rejected during processing and thinking tokens when reported. Failed
requests or interrupted streams without usage metadata cannot be accounted for;
therefore even a complete observed summary may be lower than the provider bill.
The translation observer also depends on an upstream private method and falls
back to the engine summary if unavailable. Estimates do not account for cache discounts, modality-specific rates,
or pricing tiers; they are not a billing statement. Update rates when changing models.

### `gemini-subtrans: executable not found`

Make sure installation was performed in the same virtual environment used to start FastAPI:

```bash
source .venv/bin/activate
python -m pip install -e "vendor/llm-subtrans[gemini]"
which gemini-subtrans
```

### Transcription provider error

List providers supported by the checked-out LLM-Subtrans version:

```bash
source .venv/bin/activate
python vendor/llm-subtrans/scripts/transcribe.py --list-providers
```

If the provider is named differently in that version, update `TRANSCRIPTION_PROVIDER` in `.env`.

### Model not available / quota errors

Check the model ID and API access with the gateway provider. The project defaults are examples, not a guarantee that every model is enabled for every account. Gemini transcription may have strict rate limits; reduce `MAX_WORKERS` to 1 while testing.

### FFmpeg

```bash
ffmpeg -version
ffprobe -version
```

## Production hardening before deployment

1. Replace the in-memory job dictionary with Redis/BullMQ or a durable database.
2. Store uploaded and generated files in R2; pass signed URLs instead of large files between NestJS and Python.
3. Add service-to-service authentication and request signing.
4. Add per-user quota, idempotency, cancellation, retry and timeout policies.
5. Add file cleanup, max duration/file size, MIME sniffing and malware checks.
6. Limit concurrency based on Gemini rate limits.
7. Persist progress and result metadata.
8. Do not expose raw filesystem paths to mobile clients; return authenticated download URLs.

# Receipt Parsing Service

A backend service that ingests receipt images, performs OCR and structured data
extraction using external AI services, and validates results against a strict
schema before returning structured receipt data via a REST API.

This project focuses on data pipelines, validation, and backend service design,
with a lightweight frontend used only for upload and review.

## Live link (find front-end repo [Here](https://github.com/anujkpatty/receipt-parser-fe))
https://receipt-parser-fe.pages.dev/

## Overview

The service accepts an image of a receipt, preprocesses it for readability,
extracts raw text using an OCR service, and converts the unstructured text into
structured receipt data (items, totals, dates, payment method). To ensure
correctness and reliability, all extracted data is validated against a strict
schema before being returned to the client.

Key design goals:

- Keep business logic in the backend.
- Treat AI output as untrusted input.
- Enforce schema correctness and fail fast on invalid data.

## Usage

1. Upload receipt to get individual items

<img src="screenshots/sc1.png" alt="Architecture" height="500px">
<img src="screenshots/sc2.png" alt="Architecture" height="500px">

2. Select items to create groups

<img src="screenshots/sc3.png" alt="Architecture" height="500px">
<img src="screenshots/sc4.png" alt="Architecture" height="500px">

## Architecture

```
Client
  |
  |  (multipart image upload)
  v
Flask API
  |
  |-- Image preprocessing (resize, orientation fix)
  |-- OCR extraction (Azure Computer Vision)
  |-- Structured parsing (LLM)
  |-- Schema validation (Pydantic)
  v
JSON Response
```

## Components

- API Layer (Flask): handles image uploads and HTTP responses; exposes health and
  mock endpoints.
- Image Preprocessing: fixes EXIF orientation and resizes images for more
  reliable OCR.
- OCR Module: extracts raw text using Azure Computer Vision.
- Structured Parsing: uses a large language model to convert raw text into
  structured data.
- Validation Layer: enforces schema correctness using Pydantic and rejects
  malformed or incomplete outputs.

## API Endpoints

### POST /parse

Parses a receipt image and returns structured receipt data.

Request:

- `multipart/form-data`
- Field: `image` (receipt image)

Response example:

```json
{
  "store_name": "Example Store",
  "store_address": "123 Main St",
  "store_number": 42,
  "items": [
    { "name": "Milk", "price": 3.49 },
    { "name": "Bread", "price": 2.99 }
  ],
  "tax": 0.54,
  "total": 6.48,
  "date": "2024-01-15",
  "payment_method": "credit"
}
```

Example:

```sh
curl -X POST http://localhost:8080/parse \
  -F "image=@test_receipt.jpeg"
```

### GET /health

Health check endpoint.

```json
{ "status": "healthy" }
```

### POST /mock

Returns static mock receipt data (used for frontend development).

## Expense Tracking Agent

After a user reviews a parsed receipt, the frontend can send it to
`POST /expenses/receipts` to track it. Each request runs three steps
(`expense_agent.py`):

1. **Categorize**: Gemini sorts each item into a category (`alcohol`,
   `beverages`, `food`, `groceries`, `household`, `personal_care`,
   `entertainment`, `transportation`, `other`). The output is validated, and
   anything it doesn't recognize becomes `other`.
2. **Spending rules**: fixed checks with no LLM involved. Each rule has a
   category (or none, meaning total spend), a period (`receipt`, `day`, `week`
   or `month`) and a limit. A flag is raised when a receipt adds spend to a
   bucket that ends up over its limit.
3. **Anomaly agent**: Gemini calls read-only tools over the user's history
   (`get_spending_summary`, `get_category_history`, `get_recent_receipts`).
   Through `flag_expense` it can raise up to 3 flags for patterns that rules
   don't cover, such as a spike against the user's usual weekly spend. This
   step can fail without failing the request. Turn it off with
   `EXPENSE_AGENT_ENABLED=false`.

Data is stored in SQLite (`EXPENSES_DB_PATH`, default `expenses.db`). Every
endpoint needs an `X-User-Id` header (letters, digits, `_` or `-`, up to 64
characters).

| Endpoint | Description |
| --- | --- |
| `POST /expenses/receipts` | Body: a receipt in the `/parse` format. Returns the categorized items, the flags and a short summary. |
| `GET /expenses/summary?period=month` | Spend by category for the current `day`, `week` or `month`. |
| `GET /expenses/rules` / `POST /expenses/rules` | List or create rules, e.g. `{"category": "alcohol", "period": "week", "limit": 75}`. |
| `DELETE /expenses/rules/<id>` | Delete a rule. |
| `GET /expenses/flags[?include_acknowledged=true]` | List flags. |
| `POST /expenses/flags/<id>/acknowledge` | Dismiss a flag. |

Run the tests with `pip install pytest && PYTHONPATH=. pytest tests`.

## Data Model and Validation

Receipt data is validated using Pydantic models to ensure correctness:

- Required fields: store name, items, payment method.
- Optional fields: tax, total, date, store number.
- Dates must match `YYYY-MM-DD`.
- Payment method must be one of: `cash`, `credit`, `debit`, `check`, `other`.

Invalid or incomplete data is rejected with a clear error response. This
validation layer prevents malformed AI output from propagating to consumers.

## Running Locally

Prerequisites:

- Python 3.9+
- Node.js (optional, for the frontend)
- API keys for external services

Environment variables:

```sh
export VISION_ENDPOINT="your-azure-endpoint"
export VISION_KEY="your-azure-vision-key"
export GEMINI_API_KEY="your-gemini-api-key"
```

Backend:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If you do not have a `requirements.txt`, install dependencies directly:

```sh
pip install flask flask-cors pillow pydantic google-generativeai azure-ai-vision-imageanalysis
```

Run the API:

```sh
python app.py
```

The backend will run on `http://localhost:8080`.

Frontend (optional):

```sh
cd frontend
npm install
npm run dev
```

## Cloud Run and Secret Manager

Use Secret Manager to avoid committing API keys. Create secrets, grant the Cloud
Run service account access, then reference them in `cloudrun.yaml`.

Create secrets:

```sh
echo -n "YOUR_GEMINI_KEY" | gcloud secrets create GEMINI_API_KEY --data-file=-
echo -n "YOUR_AZURE_ENDPOINT" | gcloud secrets create VISION_ENDPOINT --data-file=-
echo -n "YOUR_AZURE_KEY" | gcloud secrets create VISION_KEY --data-file=-
```

Grant access (replace with your service account if different):

```sh
gcloud secrets add-iam-policy-binding GEMINI_API_KEY \
  --member="serviceAccount:121187258964-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding VISION_ENDPOINT \
  --member="serviceAccount:121187258964-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding VISION_KEY \
  --member="serviceAccount:121187258964-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

Deploy:

```sh
gcloud run services replace cloudrun.yaml --region us-central1
```

## Design Decisions and Tradeoffs

- Synchronous processing: OCR and parsing are performed synchronously for
  simplicity. In production, this would move to an async job queue with
  background workers.
- Schema validation: AI output is treated as untrusted input. Invalid responses
  are rejected rather than partially accepted.
- No authentication: omitted to keep the project focused on parsing and
  validation logic.
- Local execution: designed to run locally for easy iteration and testing.

## Future Improvements

- Move parsing to an async worker (Celery or a task queue).
- Add retries and timeout handling for external services.
- Introduce request tracing and metrics.
- Persist parsed receipts to a database.
- Containerize and deploy via Kubernetes.

## Implementation Notes

- The server writes a resized image to `processed_output.png` on each parse.
- `ocr_module.py` exits if `VISION_ENDPOINT` or `VISION_KEY` is not set.

## Why This Project

This project was built to explore:

- Designing backend processing pipelines.
- Integrating OCR and AI services.

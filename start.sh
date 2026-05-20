#!/bin/bash
set -e

# FastAPI runs internally; Streamlit is the public-facing service on $PORT
uvicorn api:app --host 0.0.0.0 --port 8000 &

exec streamlit run app.py \
  --server.port "${PORT:-8501}" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --browser.gatherUsageStats false

# Runs the evaluation harness and its dashboard in a container.
#
#   docker build -t deriv-eval .
#   docker run --rm -p 127.0.0.1:5050:5050 deriv-eval        # dashboard
#   docker run --rm deriv-eval python run.py                 # pipeline only
#   docker run --rm deriv-eval python validate.py            # validation only
#   docker run --rm deriv-eval python -m pytest -q           # test suite
#
# See the README's "Running with Docker" section for volume mounts and
# enabling the live LLM judge with your own API key.

FROM python:3.12-slim

# PYTHONUNBUFFERED so pipeline output appears immediately in `docker logs`
# rather than sitting in a buffer; PYTHONDONTWRITEBYTECODE to keep the
# image free of .pyc files that would only ever be written once.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies are copied and installed before the source so that editing
# code does not invalidate the (slow) pip layer on rebuild.
COPY requirements.txt requirements-dev.txt requirements-frontend.txt requirements-llm.txt ./
RUN pip install --no-cache-dir \
        -r requirements-frontend.txt \
        -r requirements-dev.txt \
        -r requirements-llm.txt

# The harness, the dashboard, the tests, the config, and the sample inputs.
COPY evalharness/ ./evalharness/
COPY frontend/ ./frontend/
COPY tests/ ./tests/
COPY run.py validate.py config.yaml ./
COPY kb.json queries.json candidate_answers.json ./

# The cached judge response, so the container reproduces the real LLM
# verdicts offline with no API key. Without this the judge would fall
# back to the deterministic stub.
COPY llm_calls.jsonl ./

# Run as a non-root user. /app is owned by that user because the
# pipeline writes its artifacts there, and the dashboard's "Run Pipeline"
# button rewrites them at runtime.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Generate the artifacts at build time so the dashboard has something to
# show the moment the container starts. Uses the cached judge response
# copied above, so this makes no network calls.
RUN python run.py

EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5050/api/state', timeout=4).status==200 else 1)"

# 0.0.0.0 is required for Docker port publishing to reach the process --
# 127.0.0.1 inside a container is only reachable from inside it. Restrict
# exposure on the host side instead: -p 127.0.0.1:5050:5050
CMD ["uvicorn", "frontend.server:app", "--host", "0.0.0.0", "--port", "5050"]

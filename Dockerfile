# TastePilot hosted demo — standard library only, nothing to pip install.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml LICENSE README.md ./
COPY tastepilot/ tastepilot/

# Run unprivileged; the demo needs no writable filesystem.
RUN useradd --create-home --uid 10001 tastepilot
USER tastepilot

ENV PORT=8765
EXPOSE 8765

# The Qloo key, when used at all, comes from the platform's secret env
# (QLOO_API_KEY) at runtime — it is never baked into the image (.env is
# excluded via .dockerignore).
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python3 -c "import json,os,urllib.request; json.load(urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/api/health', timeout=4))"

CMD ["python3", "-m", "tastepilot", "serve", "--host", "0.0.0.0", "--mode", "auto"]

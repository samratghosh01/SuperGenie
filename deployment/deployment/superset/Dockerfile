FROM apache/superset:5.0.0

USER root
RUN uv pip install --python /app/.venv/bin/python psycopg2-binary==2.9.9
RUN mkdir -p /app/uploads && chown superset:superset /app/uploads

# Replace default Superset logo with custom branding (provide your own logo.png)
COPY static/logo.png /app/.venv/lib/python3.10/site-packages/superset/static/assets/images/superset-logo-horiz.png

USER superset

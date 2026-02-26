#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# init.sh — runs inside the Superset container on startup
# Steps: DB upgrade → admin → init → load CSV → start Superset → create dataset
# ─────────────────────────────────────────────────────────────────────────────
set -e

# Superset 5.0.0 uses a venv — activate it so superset CLI + python3 both work
export PATH="/app/.venv/bin:$PATH"

# Read credentials from environment (with defaults for dev)
ADMIN_PASS="${SUPERSET_ADMIN_PASSWORD:-admin}"
PG_PASS="${POSTGRES_PASSWORD:-superset}"

echo ""
echo "==> [1/6] Running DB migrations ..."
superset db upgrade

echo ""
echo "==> [2/6] Creating admin user ..."
superset fab create-admin \
    --username admin \
    --firstname Admin \
    --lastname User \
    --email admin@example.com \
    --password "$ADMIN_PASS" 2>/dev/null || echo "     (admin already exists)"

echo ""
echo "==> [3/6] Running superset init ..."
superset init

echo ""
echo "==> [4/6] Loading sample data into Postgres ..."
python3 << PYEOF
import psycopg2, csv, os, sys

conn = psycopg2.connect("postgresql://superset:${PG_PASS}@db:5432/superset")
conn.autocommit = True
cur = conn.cursor()

cur.execute("CREATE SCHEMA IF NOT EXISTS app_data")
cur.execute("DROP TABLE IF EXISTS app_data.sales_data")
cur.execute("""
    CREATE TABLE app_data.sales_data (
        order_id       INTEGER,
        order_date     DATE,
        region         VARCHAR(50),
        country        VARCHAR(50),
        product_category VARCHAR(50),
        product_name   VARCHAR(100),
        customer_type  VARCHAR(50),
        sales_amount   FLOAT,
        units_sold     INTEGER,
        status         VARCHAR(20)
    )
""")

csv_path = "/app/data/sales_data.csv"
count = 0
with open(csv_path) as f:
    for row in csv.DictReader(f):
        cur.execute(
            "INSERT INTO app_data.sales_data VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (int(row['order_id']), row['order_date'], row['region'], row['country'],
             row['product_category'], row['product_name'], row['customer_type'],
             float(row['sales_amount']), int(row['units_sold']), row['status']))
        count += 1

cur.close()
conn.close()
print(f"     Loaded {count} rows into app_data.sales_data")
PYEOF

echo ""
echo "==> [5/8] Creating test user 'analyst' (Gamma role) ..."
superset fab create-user \
    --role Gamma \
    --username analyst \
    --firstname Test \
    --lastname Analyst \
    --email analyst@example.com \
    --password analyst 2>/dev/null || echo "     (analyst already exists)"

echo ""
echo "==> [6/8] Starting Superset web server (background) ..."
gunicorn \
    --bind 0.0.0.0:8088 \
    --workers 2 \
    --timeout 120 \
    --limit-request-line 0 \
    "superset.app:create_app()" &
GUNICORN_PID=$!

echo "     Waiting for Superset to be healthy ..."
until python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8088/health')" 2>/dev/null; do
    sleep 3
    printf "."
done
echo ""
echo "     Superset is up!"

echo ""
echo "==> [7/8] Creating Superset database connection and dataset ..."
python3 << PYEOF
import requests, json, time

URL = "http://localhost:8088"
ADMIN_PASS = "${ADMIN_PASS}"

# Authenticate
session = requests.Session()
r = session.post(f"{URL}/api/v1/security/login",
    json={"username": "admin", "password": ADMIN_PASS, "provider": "db", "refresh": True})
r.raise_for_status()
session.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

r = session.get(f"{URL}/api/v1/security/csrf_token/")
r.raise_for_status()
session.headers.update({
    "X-CSRFToken": r.json()["result"],
    "Content-Type": "application/json",
    "Referer": URL,
})

PG_PASS = "${PG_PASS}"

# Create or reuse database connection
r = session.get(f"{URL}/api/v1/database/",
    params={"q": json.dumps({"filters": [{"col": "database_name", "opr": "eq", "value": "App Data"}]})})
items = r.json().get("result", [])
if items:
    db_id = items[0]["id"]
    print(f"     Database 'App Data' exists (id={db_id}), reusing.")
else:
    r = session.post(f"{URL}/api/v1/database/", json={
        "database_name": "App Data",
        "sqlalchemy_uri": f"postgresql+psycopg2://superset:{PG_PASS}@db:5432/superset",
        "expose_in_sqllab": True,
        "allow_file_upload": True,
        "extra": json.dumps({"metadata_params": {}, "engine_params": {}, "schemas_allowed_for_file_upload": ["app_data", "public"]}),
    })
    r.raise_for_status()
    db_id = r.json()["id"]
    print(f"     Created database 'App Data' (id={db_id})")

# Create or reuse dataset
r = session.get(f"{URL}/api/v1/dataset/",
    params={"q": json.dumps({"filters": [{"col": "table_name", "opr": "eq", "value": "sales_data"}]})})
items = r.json().get("result", [])
if items:
    print(f"     Dataset 'sales_data' exists (id={items[0]['id']}), reusing.")
else:
    r = session.post(f"{URL}/api/v1/dataset/", json={
        "database": db_id,
        "schema": "app_data",
        "table_name": "sales_data",
    })
    r.raise_for_status()
    print(f"     Created dataset 'sales_data' (id={r.json()['id']})")

print("     Dataset setup complete!")
PYEOF

echo ""
echo "==> [8/8] Granting dataset permissions to Gamma role ..."
python3 << 'PYEOF'
import psycopg2, os

PG_PASS = os.environ.get("POSTGRES_PASSWORD", "superset")
conn = psycopg2.connect(f"postgresql://superset:{PG_PASS}@db:5432/superset")
conn.autocommit = True
cur = conn.cursor()

# Grant Gamma role access to the sales_data dataset via permission_view_role table
cur.execute("""
    SELECT pv.id FROM ab_permission_view pv
    JOIN ab_permission p ON pv.permission_id = p.id
    JOIN ab_view_menu vm ON pv.view_menu_id = vm.id
    WHERE p.name = 'datasource_access'
    AND vm.name LIKE '%%sales_data%%'
""")
pv_rows = cur.fetchall()

cur.execute("SELECT id FROM ab_role WHERE name = 'Gamma'")
gamma_row = cur.fetchone()

if gamma_row and pv_rows:
    gamma_id = gamma_row[0]
    for (pv_id,) in pv_rows:
        # Check if already granted
        cur.execute("""
            SELECT 1 FROM ab_permission_view_role
            WHERE permission_view_id = %s AND role_id = %s
        """, (pv_id, gamma_id))
        if not cur.fetchone():
            cur.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM ab_permission_view_role")
            next_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO ab_permission_view_role (id, permission_view_id, role_id)
                VALUES (%s, %s, %s)
            """, (next_id, pv_id, gamma_id))
            print(f"     Granted permission_view {pv_id} to Gamma role (id={next_id})")
        else:
            print(f"     Permission_view {pv_id} already granted to Gamma")
    print(f"     Gamma role can now access sales_data")
else:
    if not gamma_row:
        print("     Gamma role not found in DB")
    if not pv_rows:
        print("     No datasource_access permission for sales_data yet (this is OK on first run)")

cur.close()
conn.close()
print("     Permission grants complete!")
PYEOF

echo ""
echo "============================================================"
echo "  Superset is ready!"
echo "  admin  / [your password]  — full access"
echo "  analyst / analyst         — Gamma role (sales_data only)"
echo "============================================================"

wait $GUNICORN_PID

# BI Genie — AI-Powered Dashboard Builder for Apache Superset

An AI chatbot embedded inside Apache Superset that builds dashboards from natural language. Users describe what they want, the AI proposes visualizations, and the system automatically creates multi-chart dashboards — all within seconds.

![Architecture](https://img.shields.io/badge/Superset-5.0.0-blue) ![Python](https://img.shields.io/badge/Python-3.11-green) ![License](https://img.shields.io/badge/License-MIT-yellow)

## Features

- **Natural Language to Dashboard** — Describe charts in plain English, get production-ready Superset dashboards
- **Multi-Chart Dashboards** — Creates up to 6 charts per dashboard with automatic grid layout
- **RBAC-Aware** — Inherits logged-in user's Superset permissions; only shows accessible datasets
- **Embedded Chat Widget** — Floating chat button injected into every Superset page
- **Session Persistence** — Chat history survives page refreshes with 30-min inactivity timeout
- **Custom Branding** — Replace Superset logos with your own via MutationObserver
- **CSV/Excel Upload** — Upload data files and immediately use them in AI-generated dashboards

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Docker Compose                        │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────┐ │
│  │ Superset  │  │ FastAPI  │  │ Postgres │  │ Redis  │ │
│  │ :9088     │──│ :9000    │──│ :5432    │  │ :6379  │ │
│  │           │  │ (LLM +   │  │          │  │        │ │
│  │ Chat      │  │  Superset│  │          │  │        │ │
│  │ Widget JS │  │  API)    │  │          │  │        │ │
│  └──────────┘  └──────────┘  └──────────┘  └────────┘ │
└─────────────────────────────────────────────────────────┘
```

**Flow:**
1. Superset injects chat widget JS into every page (via `FLASK_APP_MUTATOR`)
2. Widget JS fetches user identity + permitted datasets from Superset API (RBAC)
3. User context is sent to chat iframe via `postMessage`
4. Chat UI forwards messages + user context to FastAPI backend
5. Backend uses LLM to propose charts, then creates them via Superset REST API
6. Charts are linked to dashboards via direct PostgreSQL inserts

## Quick Start

### Prerequisites

- Docker and Docker Compose
- An LLM API key (OpenAI, Anthropic, or LiteLLM proxy)

### Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/bi-genie.git
   cd bi-genie
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your LLM API key and other settings
   ```

3. **Add your logo** (optional):
   ```bash
   # Place your logo files:
   cp your-logo.png backend/logo.png
   cp your-logo.png superset/static/logo.png
   ```

4. **Start the stack:**
   ```bash
   docker compose up --build -d
   ```

5. **Wait for initialization** (~2-3 minutes for first start):
   ```bash
   docker compose logs -f superset
   # Wait until you see "Superset is ready!"
   ```

6. **Open Superset:**
   - URL: http://localhost:9088
   - Admin login: `admin` / (password from your `.env`)
   - Test user: `analyst` / `analyst` (Gamma role, limited dataset access)

7. **Click the chat button** (blue circle, bottom-right) and start building dashboards!

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `LITELLM_API_KEY` | API key for your LLM provider | (required) |
| `LITELLM_URL` | Base URL for LLM API | (required) |
| `LLM_MODEL` | Model name | `claude-haiku-4-5@20251001` |
| `SUPERSET_ADMIN_PASSWORD` | Superset admin password | `admin` |
| `SUPERSET_SECRET_KEY` | Superset Flask secret key | `change-me-in-production` |
| `POSTGRES_PASSWORD` | PostgreSQL password | `superset` |
| `BACKEND_URL` | External URL of the backend | `http://localhost:9000` |
| `SUPERSET_EXTERNAL_URL` | External URL of Superset | `http://localhost:9088` |
| `LLM_SKIP_TLS_VERIFY` | Skip TLS verification for LLM | `false` |

### LLM Provider Setup

BI Genie uses the OpenAI-compatible API format. You can use:

- **OpenAI directly**: Set `LITELLM_URL=https://api.openai.com/v1`
- **Anthropic via LiteLLM**: Deploy a [LiteLLM proxy](https://docs.litellm.ai/)
- **Any OpenAI-compatible API**: Set the base URL accordingly

### Custom Data

Replace `data/sales_data.csv` with your own CSV file. Update the table schema in `superset/init.sh` to match your data columns.

## EC2 / Production Deployment

For production deployments:

1. **Change all default passwords** in `.env`
2. **Generate a strong `SUPERSET_SECRET_KEY`**: `openssl rand -base64 42`
3. **Update URLs** in `.env` to use your server's IP or domain
4. **Consider adding nginx** as a reverse proxy with SSL termination
5. **Set `TALISMAN_ENABLED = True`** in `superset_config.py` if using HTTPS

### Minimum EC2 Requirements

- Instance: `t3.medium` (2 vCPU, 4GB RAM)
- Storage: 20GB EBS (for PostgreSQL data)
- Security group: ports 9088 (Superset) and 9000 (backend), or 80/443 behind nginx

## Project Structure

```
.
├── docker-compose.yml          # Orchestrates all 4 services
├── .env.example                # Environment variable template
├── backend/
│   ├── Dockerfile              # Python 3.11 slim
│   ├── main.py                 # FastAPI app (LLM + Superset API orchestration)
│   ├── chat_ui.html            # Chat interface (served as iframe)
│   ├── requirements.txt        # Python dependencies
│   └── logo.png                # Custom logo (provide your own)
├── superset/
│   ├── Dockerfile              # Superset 5.0.0 + psycopg2
│   ├── superset_config.py      # Superset config + chat widget injection
│   ├── init.sh                 # DB migrations, user setup, data loading
│   └── static/
│       └── logo.png            # Custom logo for Superset navbar
└── data/
    └── sales_data.csv          # Sample dataset
```

## How It Works

### RBAC Enforcement

The chat widget JS runs in Superset's origin (same-origin), so it can call Superset's RBAC-aware REST API using the logged-in user's session cookies:

1. `GET /api/v1/me/` — identifies the user
2. `GET /api/v1/dataset/` — returns only datasets the user can access
3. Results are sent to the chat iframe via `postMessage`
4. Backend verifies the user exists and cross-references claimed datasets

### Dashboard Creation

When a user confirms a proposal:

1. LLM outputs structured JSON with chart specifications
2. Backend creates each chart via `POST /api/v1/chart/`
3. Creates a dashboard via `POST /api/v1/dashboard/` with position layout
4. Links charts to dashboard via direct PostgreSQL insert (Superset REST API doesn't auto-link)
5. Sets `owners` to the logged-in user for proper RBAC on created objects

## License

MIT

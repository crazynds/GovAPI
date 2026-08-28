# Gov API

> Open API for Brazilian government public data — CNPJ/CNAE, municipalities, ZIP codes/addresses, and states.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## What's inside

| Dataset | Source | What you get |
|---|---|---|
| **CNPJ / CNAE** | Federal Revenue's public CNPJ database | Company search by CNAE (business activity), state, region, size, MEI/Simples status, etc. |
| **Municipalities** | Official IBGE/Revenue list | Municipality lookup by name, state, region, or code. |
| **CEP / Address** | [e-DNE Básico](https://github.com/cauethenorio/edne-correios-loader) | Address lookup by ZIP code or free text. Falls back to [ViaCEP](https://viacep.com.br) for anything not yet covered, and saves the result for next time. |
| **States** | Static list | Brazilian states (UF + name + region). |
| **Taxes (Simples Nacional)** | LC 123/2006 Anexos I–V | Effective tax rate and DAS amount calculation, Fator R calculation. Pure math, no import needed. |

Originally built to power a B2B prospecting system without keeping this data in the main application database. Published here because it may be useful for any project that needs it without reinventing the import pipeline.

Contributions adding other public data sources (IBGE, tax records, public tenders, etc.) are welcome.

---

## Table of contents

- [Requirements](#requirements)
- [Quick start (local)](#quick-start-local)
- [Deploying to production](#deploying-to-production)
- [Configuration](#configuration)
- [Importing the data](#importing-the-data)
- [Database migrations](#database-migrations)
- [API reference](#api-reference)
- [Project structure](#project-structure)
- [License](#license)

---

## Requirements

- Docker + Docker Compose v2
- A Postgres database — either the one bundled in `docker-compose.yml` or your own

## Quick start (local)

Uses the Postgres bundled in the compose file.

```bash
git clone <this-repo> && cd dados-gov-br
cp .env.example .env
docker compose --profile local-db up -d
```

The API is now at `http://localhost:8000` — `/docs` for Swagger, `/health` for a liveness check. It's empty until you [import some data](#importing-the-data).

## Deploying to production

The same `docker-compose.yml` runs in production, just with different environment variables.

1. **Provision a server** with Docker and Docker Compose installed.
2. **Point at a real Postgres** — in `.env`, set:
   ```bash
   APP_DB_HOST=your-db-host
   APP_DB_PORT=5432
   APP_DB_USER=user
   APP_DB_PASSWORD=password
   APP_DB_NAME=cnpj
   ```
3. **Bring up just the app** — skip `--profile local-db` so the bundled `db` container never starts:
   ```bash
   cp .env.example .env
   $EDITOR .env   # set APP_DB_* (step 2) and anything else you need
   docker compose up -d app
   ```
   It applies pending database migrations on boot — see [Database migrations](#database-migrations).
4. **Put a reverse proxy in front of it** for TLS and a domain, e.g. with [Caddy](https://caddyserver.com):
   ```
   api.yourdomain.com {
       reverse_proxy localhost:8000
   }
   ```
5. **Run the first import** — see [Importing the data](#importing-the-data).
6. **Lock down `POST /import/trigger`** at the reverse proxy if the API is public — it has no authentication.

The app's code (`app/`, `alembic/`) is bind-mounted from the host, not baked into the image — after `git pull`, restart the container to pick up the change:

```bash
git pull
docker compose restart app
```

Rebuild the image only when `requirements.txt` changes (dependencies do live in the image):

```bash
docker compose build app
docker compose up -d app
```

## Configuration

Set via `.env` (copy `.env.example` to start).

| Variable | Default | Purpose |
|---|---|---|
| `APP_PORT` | `8000` | Host port the API is exposed on. |
| `APP_DB_HOST` | `db` | Postgres host. |
| `APP_DB_PORT` | `5432` | Postgres port. |
| `APP_DB_USER` | `cnpj` | Postgres user. |
| `APP_DB_PASSWORD` | `cnpj` | Postgres password. |
| `APP_DB_NAME` | `cnpj` | Postgres database name. |
| `APP_DOWNLOAD_DIR` | `/data/cnpj-import` | Scratch directory for files during import. |

## Importing the data

One command imports everything:

```bash
docker compose run --rm app python -m app.cli import-all
```

Or import each source independently:

```bash
# CNPJ (Federal Revenue)
docker compose run --rm app python -m app.cli import-cnpj

# CNPJ — just one stage (reference, simples, empresas, estabelecimentos, build)
docker compose run --rm app python -m app.cli import-cnpj --only estabelecimentos

# CEPs (Post Office e-DNE Básico)
docker compose run --rm app python -m app.cli import-ceps
```

Neither runs automatically — schedule `import-all` yourself (e.g. cron, an external scheduler) if you want periodic refreshes. Progress:

```bash
curl http://localhost:8000/import/status
```

## Database migrations

Schema is managed with [Alembic](https://alembic.sqlalchemy.org/). `app` applies pending migrations automatically on boot (see `docker-entrypoint.sh` / `app/migrate.py`).

To run migrations by hand:

```bash
docker compose run --rm app python -m app.cli migrate
```

To generate a new migration after changing `app/models.py`:

```bash
docker compose run --rm -v "$PWD/alembic:/srv/alembic" app alembic revision --autogenerate -m "describe the change"
```

Note: `correios_cep` (the e-DNE table) is **not** managed by Alembic — it's owned by `edne-correios-loader`, which rebuilds it on every `import-ceps` run.

## API reference

Full interactive docs (Swagger) at `/docs`.

### CNPJ

| Method | Path | Description |
|---|---|---|
| `GET` | `/establishments` | Search companies. Filters: `cnae_codes` (+ `cnae_match=any\|all`), `uf`, `regiao`, `municipio_codes`, `company_size`, `is_mei`, `is_simples`, `is_headquarters`, `name`, `has_phone`, `only_with_cellphone`, `only_with_email`, `opened_after`/`opened_before`; sortable via `sort_by`/`sort_dir`, paginated via `page`/`per_page`. Results include CNAE descriptions and a human-readable company-size label. |
| `GET` | `/establishments/by-cnpj` | Look up specific companies by CNPJ (`cnpjs=...`, repeatable). |
| `GET` | `/establishments/stats` | Aggregates over the same filters as above: totals, breakdown by state/region/company size, and top CNAE codes — useful for sizing a segment before paginating individual results. |
| `GET` | `/cnaes/search-by-description` | Search CNAE codes by description (`words=...`, repeatable). |
| `GET` | `/cnaes/codes` | List all CNAE codes. |
| `GET` | `/municipios/search` | Search municipalities by `name`, `uf`, and/or `regiao`. |
| `GET` | `/municipios/by-code/{receita_code}` | Look up a municipality by its Revenue/IBGE code. |
| `GET` | `/import/status` | Check import progress. |
| `POST` | `/import/trigger` | Trigger an import in the background. Unauthenticated. |

### Address

| Method | Path | Description |
|---|---|---|
| `GET` | `/enderecos/cep/{cep}` | Look up an address by ZIP code. |
| `GET` | `/enderecos/buscar` | Free-text address search — `logradouro` (street), `bairro` (neighborhood), `municipio`, `uf` (repeatable), `regiao`, `municipio_cod_ibge`; paginated via `page`/`per_page`. |
| `GET` | `/enderecos/estados` | List all states. |

### Taxes (Simples Nacional)

Pure calculation, no database involved — reference tables, not tax advice.

| Method | Path | Description |
|---|---|---|
| `GET` | `/impostos/simples/anexos` | List the 5 Simples Nacional annexes (I–V) and what each covers. |
| `GET` | `/impostos/simples/anexos/{anexo}` | Full bracket table (RBT12 ranges, nominal rate, deduction) for one annex. |
| `GET` | `/impostos/simples/calcular` | Calculate the effective rate and DAS amount — `anexo`, `rbt12` (revenue over the last 12 months), optional `receita_mes` (defaults to `rbt12/12`). |
| `GET` | `/impostos/fator-r` | Calculate the Fator R (`folha_pagamento_12m / receita_bruta_12m`) and whether it qualifies for Anexo III instead of V (≥ 28%, per §5º-D of LC 123/2006 — applies to intellectual/regulated service activities). |

### Misc

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check. |

## Project structure

```
app/
├── main.py           # FastAPI app, router registration
├── config.py         # Settings (env vars)
├── models.py         # SQLAlchemy models
├── schemas.py        # Pydantic response models
├── db.py             # Session/engine setup
├── migrate.py        # Alembic runner used on container boot
├── cli.py            # `python -m app.cli ...` commands
├── regions.py        # UF ↔ region mapping
├── tax_tables.py      # Simples Nacional Anexos I–V (static tables)
├── importer/         # CNPJ download/extract/load pipeline
└── routers/           # API endpoints, one module per resource
alembic/               # Schema migrations
```

## License

MIT — see [LICENSE](LICENSE).

# Gov API

> Open API for Brazilian government public data — CNPJ/CNAE, municipalities, ZIP codes/addresses, and states.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## What's inside

| Dataset | Source | What you get |
|---|---|---|
| **CNPJ / CNAE** | Federal Revenue's public CNPJ database | Company search by CNAE (business activity), state, region, size, MEI/Simples status, legal nature, registration status (active/suspended/closed/etc — all companies, not just active ones), etc. |
| **Partners (Sócios)** | Federal Revenue's public CNPJ database | Company shareholders/partners — search by company or by partner name/document. |
| **Reference tables** | Federal Revenue's public CNPJ database | Legal nature, partner/officer qualification, country, and deregistration-reason codes — small lookup tables. |
| **Municipalities** | Official IBGE/Revenue list, enriched with [IBGE/SIDRA](https://servicodados.ibge.gov.br) | Municipality lookup by name, state, region, or code, with estimated population and territorial area. |
| **CEP / Address** | [e-DNE Básico](https://github.com/cauethenorio/edne-correios-loader) | Address lookup by ZIP code or free text. Falls back to [ViaCEP](https://viacep.com.br) for anything not yet covered, and saves the result for next time. Coordinates (lat/long) via [BrasilAPI](https://brasilapi.com.br), cached once looked up; radius search and distance sort fall back to the municipality's centroid ([Nominatim](https://nominatim.openstreetmap.org)) for everything else, so they work across the whole CEP base, just at city-level precision where no exact coordinate is cached yet. |
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
| `APP_DOWNLOAD_DIR` | `/tmp/cnpj-import` | Scratch directory for files during import (no need to persist it across restarts). |

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

# Municipality population/area (IBGE/SIDRA) -- run after import-cnpj at least once
docker compose run --rm app python -m app.cli import-ibge

# Municipality centroid coordinates (Nominatim/OSM) -- one-time, ~1h40 (rate-limited
# to 1 req/s), resumable. Backs the low-precision fallback in the address geo endpoints.
docker compose run --rm app python -m app.cli import-municipios-geo
```

Neither runs automatically — schedule `import-all` yourself (e.g. cron, an external scheduler) if you want periodic refreshes. Progress:

```bash
curl http://localhost:8000/import/status
```

The CNPJ import runs its three stages — download, unzip, load — in parallel, one
file per stage at a time (like `docker pull`): while a 20 GB CSV is loading, the
next archive is already downloading. `/import/status` therefore reports one entry
per stage under `stages`, each on a different file.

Because more than one file is on disk at once, admission is capped by a byte
budget rather than a file count. It defaults to 70% of the free space in
`APP_DOWNLOAD_DIR`; set `APP_DISK_BUDGET` (in bytes) to pin it. Small files
pipeline freely, and the multi-gigabyte `Estabelecimentos*.zip` degrade to
near-serial instead of filling the disk.

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

To wipe everything and start from an empty, up-to-date schema:

```bash
docker compose run --rm app python -m app.cli reset-db          # asks for confirmation
docker compose run --rm app python -m app.cli reset-db --yes    # unattended
```

This drops the whole `public` schema and reapplies the migrations. It drops the
schema rather than the mapped tables because `correios_cep` is created by
`edne-correios-loader`, outside SQLAlchemy's metadata, and a table-by-table drop
would leave it behind.

## How data is stored

Everything that is a number is stored as a number, and the CNPJ as a base-36
integer — the alphanumeric root+order fit exactly in a `BIGINT` (36¹² < 2⁶³), so
a CNPJ costs 8 bytes instead of 15 as text, and the check digits are recomputed
on output rather than stored. Phone numbers drop the constant `+55`, CNAE/state/
company-size/registration-status become integers, secondary CNAEs are an
`INTEGER[]` (`NULL` when empty, with a GIN index), and the staging tables are
`UNLOGGED`. All formatting — zero padding, `+55`, punctuation — happens at the
edges: on import in `app/importer/rows.py`, on output in the routers. The API
contract is unchanged.

Base-36 also preserves ordering, so "every establishment under root X" is a
contiguous integer range that the primary key can serve — see
`app.cnpj.basico_range`.

### Addresses

An establishment stores only its CEP (as a 4-byte integer), street number, and
complement. Street, district, municipality, and state come from the Post
Office's `correios_cep` by CEP, which is that table's primary key — so they are
not duplicated across ~63M rows.

Street and district *are* stored when the CEP cannot resolve them: a small town
is often a single locality-wide CEP with no street in the e-DNE base, and there
the Revenue's data is the only source. The build decides this per row, which is
why `import-ceps` has to run before `import-cnpj` — that is the order
`import-all` uses. Run the CNPJ import without the CEP table in place and the
address still works, it is just stored unresolved (the import warns). Each
address in the API carries a `source` field saying which way it went.

`import-ceps` upserts rather than replaces. `DneLoader.load()` deletes every row
of its target table before repopulating, which would leave the CEP base empty
mid-import and would block on any foreign key pointing at it. So the loader is
pointed at a scratch table (via the `table_names` option it already exposes) and
the merge into `correios_cep` is our own `INSERT ... ON CONFLICT DO UPDATE`; the
library never touches the real table. CEPs the Post Office retired are kept
rather than deleted — that is what makes the table safe to reference — and the
import reports how many it is holding on to.

An establishment is in exactly one of two states, never both. Either it is
linked to a CEP — `cep` is set, and street/district/municipality/state come from
`correios_cep` on read — or it is not, and the Revenue's whole address record
sits in an `address` JSONB column. Unlinked covers a missing CEP as well as one
the Post Office has never heard of (mistyped, retired, foreign): keeping such a
CEP in the column would resolve no address and would block a foreign key.

JSONB rather than more columns because these are the exception, not the rule:
the vast majority of rows match a CEP and leave `address` NULL, which costs
nothing beyond its bit in the null bitmap. Each build logs the split — look for
the `CEP: N vinculados ... N sem vínculo` line.

Because an unmatched CEP becomes NULL, and because the CEP import upserts
instead of deleting, `establishments.cep` carries a real foreign key to
`correios_cep`. The build adds it after the bulk load rather than before, so
the check is one pass at the end instead of a per-row cost across the whole
insert. `correios_cep` is an ordinary model now (`models.Cep`) — the loader only ever
populates the scratch table — so Alembic manages it like any other.

That table also absorbed `cep_coordenadas`. The two were keyed identically and
only lived apart because a coordinate column glued onto `correios_cep` would
not have survived the loader rebuilding it; that rebuild is gone. Address and
coordinate are independent halves and either may be missing — there are CEPs
with an address and no coordinate, and CEPs only the OSM extract knows — so the
address columns are nullable. The CEP import's upsert lists only the address
columns in its `DO UPDATE`, which is what keeps coordinates intact across an
`import-ceps`.

Note: `correios_cep` (the e-DNE table) is **not** managed by Alembic — it's owned by `edne-correios-loader`, which rebuilds it on every `import-ceps` run.

## API reference

Full interactive docs (Swagger) at `/docs`.

### CNPJ

| Method | Path | Description |
|---|---|---|
| `GET` | `/establishments` | Search companies. Filters: `cnae_codes` (+ `cnae_match=any\|all`), `uf`, `regiao`, `municipio_codes`, `company_size`, `is_mei`, `is_simples`, `is_headquarters`, `name`, `situacao` (registration status, code or label — includes all statuses unless filtered), `has_phone`, `only_with_cellphone`, `only_with_email`, `opened_after`/`opened_before`; sortable via `sort_by`/`sort_dir`, paginated via `page`/`per_page`. Results include CNAE, legal-nature, and deregistration-reason descriptions, plus human-readable labels for company size and registration status. |
| `GET` | `/establishments/by-cnpj` | Look up specific companies by CNPJ (`cnpjs=...`, repeatable). Accepts punctuation, a full CNPJ, or just the 8-position root. |
| `GET` | `/establishments/stats` | Aggregates over the same filters as above: totals, breakdown by state/region/company size, and top CNAE codes — useful for sizing a segment before paginating individual results. |
| `GET` | `/cnaes/search-by-description` | Search CNAE codes by description (`words=...`, repeatable). |
| `GET` | `/cnaes/codes` | List all CNAE codes. |
| `GET` | `/municipios/search` | Search municipalities by `name`, `uf`, and/or `regiao`. Includes `population` and `area_km2` once `import-ibge` has run. |
| `GET` | `/municipios/by-code/{receita_code}` | Look up a municipality by its Revenue/IBGE code. |
| `GET` | `/import/status` | Check import progress — the overall run plus one entry per pipeline stage (`stages`), since download/unzip/load run in parallel on different files. |
| `POST` | `/import/trigger` | Trigger an import in the background. Unauthenticated. |

### Partners (Sócios)

| Method | Path | Description |
|---|---|---|
| `GET` | `/socios/por-empresa/{cnpj}` | List a company's partners/shareholders — accepts a full CNPJ or just the 8-digit root. |
| `GET` | `/socios/buscar` | Search partners by `nome` and/or `documento` — find every company a person/entity is a partner in. Paginated. `documento` is an exact match: for a CPF pass only the visible digits (the Revenue masks it itself, `***123456**` → `123456`); a CNPJ is accepted with or without punctuation. |

### Reference tables

Small code/description lookups — same shape as `/cnaes`. Each has `/search?name=...`, a plain list, and `/{code}`.

| Prefix | Covers |
|---|---|
| `/naturezas-juridicas` | Legal nature (e.g. "Sociedade Empresária Limitada"). |
| `/qualificacoes-societarias` | Partner/officer role (e.g. "Administrador"). |
| `/paises` | Country codes, for foreign partners/companies. |
| `/motivos-situacao-cadastral` | Why a company was deregistered/suspended. |

### Address

| Method | Path | Description |
|---|---|---|
| `GET` | `/enderecos/cep/{cep}` | Look up an address by ZIP code, including `latitude`/`longitude` when available (fetched from BrasilAPI on first lookup, cached after). |
| `GET` | `/enderecos/buscar` | Free-text address search — `logradouro` (street), `bairro` (neighborhood), `municipio`, `uf` (repeatable), `regiao`, `municipio_cod_ibge`; paginated via `page`/`per_page`. Pass `lat`+`lon` to sort by distance instead — uses the ZIP code's own cached coordinate when available, otherwise falls back to its municipality's centroid (`import-municipios-geo`); each result's `exata` field says which one was used. |
| `GET` | `/enderecos/proximos` | ZIP codes within `raio_km` of `lat`+`lon`, nearest first. Same exact-vs-municipality-centroid fallback as above. |
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
├── importer/         # CNPJ download/extract/load pipeline, IBGE enrichment
└── routers/           # API endpoints, one module per resource
alembic/               # Schema migrations
```

## License

MIT — see [LICENSE](LICENSE).

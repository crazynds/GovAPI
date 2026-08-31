# Gov API

> Open API for Brazilian government public data — CNPJ/CNAE, municipalities, ZIP codes/addresses, and states.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## What's inside

| Dataset | Source | What you get |
|---|---|---|
| **CNPJ / CNAE** | Federal Revenue's public CNPJ database | Company search by CNAE (business activity), state, region, size, MEI/Simples status, legal nature, registration status (active/suspended/closed/etc — all companies, not just active ones), etc. |
| **Partners (Sócios)** | Federal Revenue's public CNPJ database | Company shareholders/partners — search by company or by partner name/document. |
| **Reference tables** | Federal Revenue's public CNPJ database | Legal nature, partner/officer qualification, country, and deregistration-reason codes — small lookup tables. |
| **Municipalities** | [IBGE Localidades](https://servicodados.ibge.gov.br) (exact code/name/state), enriched with [IBGE/SIDRA](https://servicodados.ibge.gov.br) | Municipality lookup by name, state, region, or code, with estimated population and territorial area. |
| **CEP / Address** | [e-DNE Básico](https://github.com/cauethenorio/edne-correios-loader) | Address lookup by ZIP code or free text. Falls back to [ViaCEP](https://viacep.com.br) for anything not yet covered, and saves the result for next time. Coordinates (lat/long) via [BrasilAPI](https://brasilapi.com.br), cached once looked up; radius search and distance sort fall back to the municipality's centroid (a static public dataset, matched by exact IBGE code) for everything else, so they work across the whole CEP base, just at city-level precision where no exact coordinate is cached yet. |
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
# Municipalities (IBGE Localidades) -- run first, everything else links to this
docker compose run --rm app python -m app.cli import-municipios

# CNPJ (Federal Revenue)
docker compose run --rm app python -m app.cli import-cnpj

# CNPJ — just one stage (reference, simples, empresas, estabelecimentos, build)
docker compose run --rm app python -m app.cli import-cnpj --only estabelecimentos

# CEPs (Post Office e-DNE Básico)
docker compose run --rm app python -m app.cli import-ceps

# Municipality population/area (IBGE/SIDRA) -- run after import-municipios
docker compose run --rm app python -m app.cli import-ibge

# Municipality centroid coordinates -- static public dataset, matched by exact
# IBGE code, one request. Backs the low-precision fallback in the address geo
# endpoints.
docker compose run --rm app python -m app.cli import-municipios-geo
```

`import-all` chains all six in dependency order: municipalities, Post Office
CEPs, bulk coordinates from the OSM extract, the Revenue's CNPJ base, then IBGE
population and municipality centroids. The order is not a preference:
`correios_cep.municipio_cod_ibge` has a real foreign key to `municipios.ibge_code`,
which only exists once municipalities are loaded first; the CNPJ build links
addresses to `correios_cep`; and the Revenue's own `Municipios.zip` (code+name,
no state, no IBGE code) matches by name against the municipality rows the first
step already created, filling in `receita_code`. Chaining the individual
commands means respecting that order yourself. The last step (municipality
centroids) fetches a static public dataset (kelvins/municipios-brasileiros) in
one request and matches by exact IBGE code — no Nominatim, no 1h40 wait, no
`--skip` flag needed.

If interrupted — Ctrl-C or a genuine failure — the next `import-all` resumes
from the phase that didn't finish rather than starting over at CEPs. Each
phase writes its own status to `import_all_run` before and after running, and
a phase already `success` is skipped. That only holds while the previous
attempt didn't fully succeed, though: once all five phases complete, the
following call treats itself as a real periodic refresh (a new CNPJ period, an
updated e-DNE) and redoes everything rather than skipping forever.

The OSM coordinates phase (2/5) is best-effort: it enriches `correios_cep`
with lat/long, nothing downstream depends on it, and the Geofabrik mirror it
downloads from does go down (`503`, seen in practice). A failure there logs a
warning and moves on to CNPJ/IBGE/geocoding rather than blocking the run —
but the overall status only becomes `success` once every phase actually is
`success` or deliberately `skipped`, so a follow-up call resumes by retrying
just that one phase instead of treating a silent OSM failure as "done" and
redoing all four other phases (CNPJ's rebuild included) from scratch.

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

`correios_cep.municipio_cod_ibge` carries its own foreign key to
`municipios.ibge_code`, same graceful-degradation rule: a code the current IBGE
list doesn't recognize (a merged or renamed municipality) becomes NULL on
upsert rather than failing. That FK is only possible because `municipios` is no
longer sourced from the Revenue's own `Municipios.zip` — that file has no state
and no IBGE code, just an internal code and a name, which is why `import-ibge`
used to match it against SIDRA by name+state (fragile: names repeat across
states). Municipalities now come from the IBGE Localidades API first
(`import-municipios`, exact code+name+state, no matching at all), and
`Municipios.zip` fills in `receita_code` afterward by matching name against
those rows — the one name-based step left, and now checked against the
authoritative municipality list itself rather than a population dataset's
labels. `receita_code` is nullable for exactly this: a name with no match, or
one that collides across states, leaves the row without it rather than
guessing wrong.

That table also absorbed `cep_coordenadas`. The two were keyed identically and
only lived apart because a coordinate column glued onto `correios_cep` would
not have survived the loader rebuilding it; that rebuild is gone. Address and
coordinate are independent halves and either may be missing — there are CEPs
with an address and no coordinate, and CEPs only the OSM extract knows — so the
address columns are nullable. The CEP import's upsert lists only the address
columns in its `DO UPDATE`, which is what keeps coordinates intact across an
`import-ceps`.

`import-ceps` shows progress for both the download and the table loads that
follow — the library itself only logs, in silence for the download and one
terse line per table for the rest, so `app/importer/edne_progress.py` drives a
progress bar off the one hook it exposes (`download_report_hook`) and off a
thin wrapper around `populate_table` that counts rows as they stream through.

`import-ceps-osm`'s own ~2GB download resumes rather than restarts. Any
failure after it — filtering, exporting, the database load — used to discard
the whole file along with everything else, so a retry paid for the network
transfer again to redo work that was entirely local. The download now sends
a `Range` header when a partial (or complete) file is already on disk, and
the cleanup on failure only removes the two derived, cheap-to-regenerate
files (the OSM filter output and the GeoJSONSeq export) — the multi-gigabyte
download survives to be resumed, or skipped outright via a 416 if it was
already complete.

Real e-DNE data doesn't respect the column widths the library's own schema
declares for it — a neighborhood name longer than the `VARCHAR(36)` it assigns
its abbreviation, for one, hit in production. `app.ceps.widen_free_text_columns`
switches every free-text column (name, address, abbreviation — anything wider
than 8 chars in the library's own schema) to `TEXT` before `.load()` creates the
tables, and `correios_cep`'s matching columns are `TEXT` for the same reason,
so a long value survives both the library's own tables and our upsert into it.
Fixed-width codes (CEP, UF/country sigla, single-char flags) are left alone.

That widening only helps a table `create_all` actually creates, and `create_all`
skips any table that already exists — so a run that dies mid-way (this one did,
on the same batch, deterministically, since the e-DNE file doesn't change
within a release) leaves its tables committed with whatever schema was current
at the time, on a connection separate from the one the failed INSERT rolled
back. The next run would silently reuse that narrow table forever. So
`import-ceps` now drops everything the library is about to (re)create before
each run, `correios_cep` itself excluded — it's a scratch table under a
different name, never part of the library's own schema.

## API reference

Full interactive docs (Swagger) at `/docs`.

### Pagination

`/establishments`, `/socios/buscar` and `/enderecos/buscar` paginate by **cursor**, not page number:

```
GET /establishments?uf=PR&limit=25
{ "data": [...], "next_cursor": "eyJ2IjoxLCJrIjpb...", "limit": 25 }

GET /establishments?uf=PR&limit=25&cursor=eyJ2IjoxLCJrIjpb...
```

Keep following `next_cursor` until it comes back `null` — that's the last page.

There is deliberately **no `total` and no page count**. Producing them means running a
`count()` over the whole result set on every request, which on a 70M+ row table costs the
same as reading everything — a `?uf=PR&per_page=1` used to time out while counting millions
of rows nobody was going to read. Cursors also make deep pages cheap: `OFFSET 50000` has to
produce and throw away 50 000 rows, while a cursor is an index seek.

The trade-off is that you can only move forward one page at a time — no jumping to an
arbitrary page, and no "page 3 of 812".

**Results always come back in primary-key order, and that isn't configurable.** These endpoints
used to accept `sort_by`/`sort_dir`; those are gone. Ordering by a non-key column forces the
database to order the entire filtered result before it can cut the page — to know which company
in Paraná has the highest `cellphone_confidence`, it has to look at every company in Paraná, and
no `limit` saves you from that. Ordering by the primary key is free: the page comes out of a
contiguous stretch of an index that already exists.

The one exception is `/enderecos`: passing `lat`+`lon` (and `/enderecos/proximos`) sorts by
distance, because "nearest first" is the entire point of those. They pay for it, so use them
only when you want proximity.

### CNAE lookups

A company has one main CNAE and any number of secondary ones, and `?cnae_codes=` matches
either. That used to be stored as `main_cnae` plus a `secondary_cnaes integer[]` column, which
made the filter `main_cnae = X OR secondary_cnaes && ARRAY[X]` — an `OR` between a btree and a
GIN index, which produces no output ordered by `cnpj`. Combined with `ORDER BY cnpj LIMIT n`,
the planner would walk the primary key filtering row by row, which on a selective slice
(`?cnae_codes=6202300&uf=RS&only_with_cellphone=true`) means reading all 63M rows — a timeout.

The relation now lives in its own table, `establishment_cnaes`: one row per (company, CNAE),
with `is_main` marking the main one. The filter becomes plain equality on one column, and an
index ending in `cnpj` hands back the page already in order, so `LIMIT` stops early instead of
sorting the whole filtered set. `establishments.main_cnae` stays — it's a dimension of the
stats aggregate and appears in every response.

That table also carries **copies** of `uf` and a `has_cellphone` flag, and the search pushes
those two filters into it. Without them the database would find candidates by CNAE and then
probe the 63M-row table one by one to discover which are in Rio Grande do Sul and have a
cellphone; with them, filter *and* order come out of a single index. Duplicating a column is
normally a maintenance liability, but no update is possible here: both tables are rebuilt from
scratch by the import and swapped in the *same* atomic `RENAME`.

Passing several codes matches companies having **at least one** of them. There used to be a
`cnae_match=all` ("has all of these") — it's gone: intersection can't be answered by merging
index ranges, so it needed a `GROUP BY … HAVING count(*) = n` that reads every row of every
code before the `LIMIT` can cut anything.

### Precomputed stats

`/establishments/stats` reads `establishments_stats`, an aggregate table keyed by state,
registration status, company size, main CNAE and the MEI/Simples/headquarters flags, with the
counts as measures. Summing a few million aggregate rows replaces counting 70M+ detail rows.

This works because `establishments` is rebuilt wholesale by the import and never written to
while it's being served: there is nothing to invalidate. The import builds the aggregate from
the same snapshot and swaps it in the same atomic `RENAME`, so the two tables can never
disagree — there's no moment where `/stats` answers about one snapshot and `/establishments`
about another.

A second table, `establishments_cnae_stats`, answers CNAE filters. It's keyed by CNAE with the
code counted as main *or* secondary, which matters more than it sounds: for CNAE 4781400 in
Paraná, 240,771 companies have it as their main activity and 397,369 have it at all — ignoring
secondary CNAEs would undercount by 39%. A company appears in one bucket per distinct CNAE it
has, so buckets can't be summed *across* CNAEs; within a single CNAE the sum is exact, and
that's the only way it's used — **one CNAE per request**. Several CNAEs at once fall back.

Requests using a filter neither aggregate carries — `name`, `opened_after`/`opened_before`,
`municipio_codes`, `has_phone`, several CNAEs, or a CNAE together with `include_breakdowns` —
fall back to the full table rather than return a fast wrong number, and can be slow.

### Text search

`?name=`, `?nome=`, `?logradouro=`, `?bairro=` and `?municipio=` are substring matches
(`ILIKE '%term%'`), backed by `pg_trgm` GIN indexes so they don't degenerate into a full scan.
Trigrams need **at least 3 characters** to be useful — a one- or two-letter term can't use the
index and falls back to scanning, so keep search terms reasonably long.

Treat the cursor as opaque. Filters and sort must stay the same across a paginated run; a
cursor used with different ones is rejected with `422` rather than silently returning an
arbitrary slice.

### CNPJ

| Method | Path | Description |
|---|---|---|
| `GET` | `/establishments` | Search companies. Filters: `cnae_codes` (matches the main CNAE or any secondary one; with several codes, matches companies having at least one of them), `uf`, `regiao`, `municipio_codes`, `company_size`, `is_mei`, `is_simples`, `is_headquarters`, `name`, `situacao` (registration status, code or label — includes all statuses unless filtered), `has_phone`, `only_with_cellphone`, `only_with_email`, `opened_after`/`opened_before`; paginated by cursor via `cursor`/`limit` (see [Pagination](#pagination)). Results include CNAE, legal-nature, and deregistration-reason descriptions, plus human-readable labels for company size and registration status. |
| `GET` | `/establishments/by-cnpj` | Look up specific companies by CNPJ (`cnpjs=...`, repeatable). Accepts punctuation, a full CNPJ, or just the 8-position root. |
| `GET` | `/establishments/stats` | Totals and breakdowns over the same filters as `/establishments`. Answered from a precomputed aggregate table rebuilt by the import, so it doesn't scan the 70M-row table at request time. Filters the aggregate doesn't carry (`name`, `opened_after`/`opened_before`, `municipio_codes`, `cnae_codes`, `only_with_cellphone`/`only_with_email`, `has_phone`) fall back to the full table and can be slow — for those, `include_breakdowns=true` is what makes it expensive. |
| `GET` | `/cnaes/search-by-description` | Search CNAE codes by description (`words=...`, repeatable). |
| `GET` | `/cnaes/codes` | List all CNAE codes. |
| `GET` | `/municipios/search` | Search municipalities by `name`, `uf`, and/or `regiao`. Includes `population` and `area_km2` once `import-ibge` has run. |
| `GET` | `/municipios/by-code/{code}` | Look up a municipality by code — either the Revenue's 4-digit code or the 7-digit IBGE code; the width decides which one is queried. |
| `GET` | `/import/status` | Check import progress — the overall run plus one entry per pipeline stage (`stages`), since download/unzip/load run in parallel on different files. |
| `POST` | `/import/trigger` | Trigger an import in the background. Unauthenticated. |

### Partners (Sócios)

| Method | Path | Description |
|---|---|---|
| `GET` | `/socios/por-empresa/{cnpj}` | List a company's partners/shareholders — accepts a full CNPJ or just the 8-digit root. |
| `GET` | `/socios/buscar` | Search partners by `nome` and/or `documento` — find every company a person/entity is a partner in. Paginated by cursor (see [Pagination](#pagination)). `documento` is an exact match: for a CPF pass only the visible digits (the Revenue masks it itself, `***123456**` → `123456`); a CNPJ is accepted with or without punctuation. |

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
| `GET` | `/enderecos/buscar` | Free-text address search — `logradouro` (street), `bairro` (neighborhood), `municipio`, `uf` (repeatable), `regiao`, `municipio_cod_ibge`; paginated by cursor via `cursor`/`limit` (see [Pagination](#pagination)). Pass `lat`+`lon` to sort by distance instead — uses the ZIP code's own cached coordinate when available, otherwise falls back to its municipality's centroid (`import-municipios-geo`); each result's `exata` field says which one was used. |
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

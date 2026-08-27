# dados-gov-br

> Open API for Brazilian government public data.

Currently covers:

- **CNPJ/CNAE** — imports the Brazilian Federal Revenue's public CNPJ (company registry) database into its own Postgres and exposes search by CNAE (business activity code), state, company size, etc.
- **Municipalities** — official list of Brazilian municipalities (IBGE/Revenue codes).
- **CEP/Address** — imports the **e-DNE Básico** (the official, free Brazilian Post Office database, ~1.6 million ZIP codes, no login required) via [edne-correios-loader](https://github.com/cauethenorio/edne-correios-loader), with search by ZIP code (CEP) and by text (street/municipality/state). Falls back to [ViaCEP](https://viacep.com.br) for ZIP codes not yet covered by e-DNE or before the import has run.
- **States** — static list of Brazilian states.

Originally built to power a B2B prospecting system without keeping this data in the main application database — published here because it may be useful for any project that needs this data without reinventing the import pipeline.

Contributions adding other public data sources (IBGE, tax records, public tenders, etc.) are welcome.

## Getting started

With the bundled Postgres from the compose file (good for local testing):

```bash
cp .env.example .env
docker compose --profile local-db up -d
```

Pointing to your own Postgres (production): edit `.env` and set `CNPJ_DATABASE_URL=postgresql+psycopg2://user:password@your-server:5432/cnpj`, then bring up just the app and scheduler (without `--profile local-db`, the bundled `db` service won't even start):

```bash
cp .env.example .env
$EDITOR .env   # uncomment and set CNPJ_DATABASE_URL
docker compose up -d app scheduler
```

## Running the CNPJ import

```bash
# manual, one-off run:
docker compose run --rm app python -m app.cli import-cnpj

# a single group (reference, simples, empresas, estabelecimentos, build):
docker compose run --rm app python -m app.cli import-cnpj --only estabelecimentos

# runs automatically on the 20th of every month at 03:00, via the `scheduler` service (started by compose already)
```

## Monitoring progress

```bash
curl http://localhost:8000/import/status
```

## Main endpoints

**CNPJ**
| Method | Path | Description |
|---|---|---|
| GET | `/establishments?cnae_codes=...&uf=...&only_with_cellphone=true&page=1&per_page=25` | Search establishments |
| GET | `/establishments/by-cnpj?cnpjs=...` | Look up establishments by CNPJ |
| GET | `/cnaes/search-by-description?words=...` | Search CNAE codes by description |
| GET | `/cnaes/codes` | List CNAE codes |
| GET | `/municipios/search?name=...` | Search municipalities |
| GET | `/import/status` | Check import status |
| POST | `/import/trigger` | Trigger an import in the background (prefer the CLI via scheduler/cron in production) |

**Address**
| Method | Path | Description |
|---|---|---|
| GET | `/enderecos/cep/{cep}` | Look up an address by ZIP code |
| GET | `/enderecos/estados` | List states |

Interactive documentation (Swagger) available at `/docs` while the server is running.

## License

MIT — see [LICENSE](LICENSE).

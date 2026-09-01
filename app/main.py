from fastapi import FastAPI

from app.routers import cnaes, addresses, establishments, taxes, import_status, municipalities, references, partners

app = FastAPI(
    title="dados-gov-br",
    description="API de dados públicos do governo brasileiro — CNPJ/CNAE da Receita Federal, municípios, endereço por CEP.",
    version="1.0.0",
)

app.include_router(establishments.router)
app.include_router(cnaes.router)
app.include_router(municipalities.router)
app.include_router(addresses.router)
app.include_router(import_status.router)
app.include_router(taxes.router)
app.include_router(references.router)
app.include_router(partners.router)


@app.get("/health")
def health():
    return {"status": "ok"}

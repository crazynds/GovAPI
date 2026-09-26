from fastapi import Depends, FastAPI

from app.auth import require_api_key
from app.routers import cnaes, addresses, establishments, taxes, import_status, municipalities, references, partners

app = FastAPI(
    title="dados-gov-br",
    description="API de dados públicos do governo brasileiro, CNPJ/CNAE da Receita Federal, municípios, endereço por CEP.",
    version="1.0.0",
)

auth = [Depends(require_api_key)]

app.include_router(establishments.router, dependencies=auth)
app.include_router(cnaes.router, dependencies=auth)
app.include_router(municipalities.router, dependencies=auth)
app.include_router(addresses.router, dependencies=auth)
app.include_router(import_status.router, dependencies=auth)
app.include_router(taxes.router, dependencies=auth)
app.include_router(references.router, dependencies=auth)
app.include_router(partners.router, dependencies=auth)


@app.get("/health")
def health():
    return {"status": "ok"}

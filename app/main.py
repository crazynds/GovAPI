from fastapi import FastAPI

from app.routers import cnaes, enderecos, establishments, import_status, municipios

app = FastAPI(
    title="dados-gov-br",
    description="API de dados públicos do governo brasileiro — CNPJ/CNAE da Receita Federal, municípios, endereço por CEP.",
    version="1.0.0",
)

app.include_router(establishments.router)
app.include_router(cnaes.router)
app.include_router(municipios.router)
app.include_router(enderecos.router)
app.include_router(import_status.router)


@app.get("/health")
def health():
    return {"status": "ok"}

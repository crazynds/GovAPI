import secrets

from fastapi import Header, HTTPException, status

from app.config import settings


def require_api_key(authorization: str = Header(...)) -> None:
    if not settings.api_key:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "APP_API_KEY nao configurada no servidor",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Chave de API invalida")

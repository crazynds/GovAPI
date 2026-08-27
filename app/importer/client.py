"""Fala com o espelho publico dos Dados Abertos de CNPJ da Receita
Federal -- autoindex Apache/Nginx classico, uma pasta YYYY-MM-DD/ por
periodo publicado."""

import re

import httpx

from app.config import settings


def base_url() -> str:
    return settings.open_data_url.rstrip("/")


def period_url(period: str) -> str:
    return f"{base_url()}/{period}/"


def discover_latest_period() -> str:
    periods = list_periods()
    if not periods:
        raise RuntimeError(f"Nenhuma pasta de período encontrada em {base_url()}")
    return sorted(periods, reverse=True)[0]


def list_periods() -> list[str]:
    html = _fetch_index(f"{base_url()}/")
    return sorted(set(re.findall(r'<a href="(\d{4}-\d{2}-\d{2})/">', html)))


def list_files(period: str) -> list[str]:
    html = _fetch_index(period_url(period))
    files = sorted(set(re.findall(r'<a href="([^"/]+\.zip)">', html, re.IGNORECASE)))
    if not files:
        raise RuntimeError(f"Nenhum arquivo .zip encontrado na pasta {period}")
    return files


def file_size(url: str) -> int | None:
    try:
        response = httpx.head(url, timeout=30, follow_redirects=True)
    except httpx.HTTPError:
        return None

    if response.is_error or "content-length" not in response.headers:
        return None

    return int(response.headers["content-length"])


def _fetch_index(url: str) -> str:
    response = httpx.get(url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    return response.text

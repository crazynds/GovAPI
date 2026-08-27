from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://cnpj:cnpj@db:5432/cnpj"

    # Mirror publico dos Dados Abertos de CNPJ da Receita Federal. A fonte
    # oficial (arquivos.receitafederal.gov.br) migrou para um portal que
    # exige login -- troque para outro mirror via env sem mudar codigo.
    #
    # Nome do campo sem o prefixo "cnpj_" de propósito: com env_prefix="CNPJ_"
    # abaixo, um campo chamado `cnpj_open_data_url` exigiria a variável
    # CNPJ_CNPJ_OPEN_DATA_URL (prefixo duplicado) e CNPJ_OPEN_DATA_URL seria
    # silenciosamente ignorada, caindo sempre no default.
    open_data_url: str = "https://dados-abertos-rf-cnpj.casadosdados.com.br/arquivos"

    # Diretorio de trabalho para zips/CSVs baixados -- limpo arquivo a
    # arquivo durante o pipeline (ver app/importer/pipeline.py).
    download_dir: str = "/data/cnpj-import"

    class Config:
        env_prefix = "CNPJ_"


settings = Settings()

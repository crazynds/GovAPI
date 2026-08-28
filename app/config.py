from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://cnpj:cnpj@db:5432/cnpj"

    # Mirror publico dos Dados Abertos de CNPJ da Receita Federal. A fonte
    # oficial (arquivos.receitafederal.gov.br) migrou para um portal que
    # exige login -- troque para outro mirror via env sem mudar codigo.
    open_data_url: str = "https://dados-abertos-rf-cnpj.casadosdados.com.br/arquivos"

    # Diretorio de trabalho para zips/CSVs baixados -- limpo arquivo a
    # arquivo durante o pipeline (ver app/importer/pipeline.py).
    download_dir: str = "/data/cnpj-import"

    class Config:
        env_prefix = "APP_"


settings = Settings()

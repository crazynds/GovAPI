from urllib.parse import quote

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Componentes do Postgres, separados em vez de uma URL única -- mais
    # facil de configurar (e de sobrescrever só uma parte, ex. só a senha)
    # via .env/variaveis de ambiente.
    db_host: str = "db"
    db_port: int = 5432
    db_user: str = "cnpj"
    db_password: str = "cnpj"
    db_name: str = "cnpj"

    # Mirror publico dos Dados Abertos de CNPJ da Receita Federal. A fonte
    # oficial (arquivos.receitafederal.gov.br) migrou para um portal que
    # exige login -- troque para outro mirror via env sem mudar codigo.
    open_data_url: str = "https://dados-abertos-rf-cnpj.casadosdados.com.br/arquivos"

    # Diretorio de trabalho para zips/CSVs baixados -- limpo arquivo a
    # arquivo durante o pipeline (ver app/importer/pipeline.py), não
    # precisa persistir entre execuções, por isso vive em /tmp sem volume.
    download_dir: str = "/tmp/cnpj-import"

    class Config:
        env_prefix = "APP_"

    @property
    def database_url(self) -> str:
        # user/password escapados -- podem ter caracteres especiais (#, %,
        # @, /, etc.) que quebrariam a URL se colados direto.
        user = quote(self.db_user, safe="")
        password = quote(self.db_password, safe="")
        # client_encoding=utf8 explicito -- sem isso, servidores cujo
        # encoding padrão não é UTF-8 (ex. banco criado como SQL_ASCII)
        # fazem o psycopg2 negociar ascii e o import quebra em qualquer
        # acento (visto na prática: UnicodeEncodeError em CNAEs/nomes).
        return (
            f"postgresql+psycopg2://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?client_encoding=utf8"
        )


settings = Settings()

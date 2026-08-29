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

    # Teto de bytes que o pipeline pode manter em `download_dir` de uma vez.
    # Os estagios (download/extract/import) rodam em paralelo, entao ha mais de
    # um zip/CSV vivo ao mesmo tempo; esse orcamento e o que impede um arquivo
    # como Estabelecimentos (~5GB zip -> ~20GB CSV) de encher o disco quando o
    # estagio seguinte esta lento. 0 = calcula 70% do espaco livre no boot.
    disk_budget: int = 0

    # `work_mem`/`maintenance_work_mem` (em MB) usados SO na sessao do build
    # (ver app/importer/pipeline.py:_build_final_table) -- nao global, pra nao
    # multiplicar por toda conexao concorrente da API e estourar RAM. O
    # default do Postgres (4MB/64MB) e pensado pra muitas conexoes pequenas
    # concorrentes, nao pra um hash join de dezenas de milhoes de linhas: sem
    # isso, o join do build (varias tabelas de ate ~29M linhas) e o CREATE
    # INDEX dos indices adiados correm risco de estourar o limite e derramar
    # em disco, que e ordens de magnitude mais lento que ficar em RAM. Suba
    # via APP_BUILD_WORK_MEM_MB se a maquina tiver RAM sobrando.
    build_work_mem_mb: int = 512
    build_maintenance_work_mem_mb: int = 1024

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

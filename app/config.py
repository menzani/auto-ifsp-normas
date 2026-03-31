from functools import lru_cache
from pydantic import model_validator
from pydantic_settings import BaseSettings

_INSECURE_SECRET = "troque-esta-chave-em-producao"


class Settings(BaseSettings):
    # Modos de desenvolvimento (False em produção; True requer opt-in explícito no .env)
    mock_auth: bool = False
    mock_bookstack: bool = False
    mock_s3: bool = False

    # Sessão
    session_secret_key: str = _INSECURE_SECRET

    @model_validator(mode="after")
    def validate_secrets(self) -> "Settings":
        if not any([self.mock_auth, self.mock_bookstack, self.mock_s3]):
            if self.session_secret_key == _INSECURE_SECRET:
                raise ValueError(
                    "SESSION_SECRET_KEY deve ser definida no .env com um valor seguro antes de rodar em modo produção."
                )
            if not self.https_only:
                raise ValueError(
                    "HTTPS_ONLY deve ser true no .env em modo produção para garantir a flag Secure no cookie de sessão."
                )
        return self

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    google_allowed_domain: str = "ifsp.edu.br"

    # Bookstack
    bookstack_base_url: str = "https://normas.ifsp.edu.br"
    bookstack_token_id: str = ""
    bookstack_token_secret: str = ""
    # ID da prateleira de staging (oculta ao público).
    # Normativos ficam aqui até serem aprovados; ao publicar são movidos
    # para a prateleira escolhida pelo usuário na hora do envio.
    bookstack_staging_shelf_id: int = 0
    # ID da prateleira de revogados (pública, listagem permanente).
    bookstack_revoked_shelf_id: int = 0

    # AWS / S3
    aws_region: str = "us-east-1"
    s3_bucket_name: str = ""
    s3_presigned_url_expiry: int = 3600  # 1 hora — janela curta reduz exposição de URLs vazadas

    # URL base da aplicação — usada para gerar links absolutos em conteúdo externo (ex: Bookstack)
    app_base_url: str = "http://localhost:8000"

    # Bedrock
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-6"

    # Administradores iniciais (emails separados por vírgula)
    # Esses usuários recebem papel "admin" automaticamente no primeiro login.
    admin_emails: str = ""

    # Sessão / segurança
    https_only: bool = False  # True em produção (HTTPS obrigatório para o cookie de sessão)

    # Limites de segurança
    max_upload_size_mb: int = 30
    max_uploads_per_user_per_hour: int = 10

    # Máximo de páginas por PDF — limita custo Bedrock Vision (controle de custo)
    max_pdf_pages: int = 100

    # Extração multimodal: Claude Vision extrai e estrutura cada lote de páginas
    multimodal_dpi: int = 120        # DPI de renderização (120 = boa legibilidade para textos menores)
    multimodal_batch_pages: int = 4  # Páginas por chamada Bedrock (menor = progresso mais frequente)


    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()

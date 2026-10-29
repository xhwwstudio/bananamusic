from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = 'MusicOn'
    app_env: str = 'development'
    database_url: str = 'sqlite:///./data/musicon.db'
    alist_url: str = 'http://alist:5244'
    alist_token: str = ''
    subsonic_user: str = 'admin'
    subsonic_pass: str = 'changeme'
    lx_service_mode: str = 'embedded'
    lx_remote_url: str = ''
    lx_embedded_enabled: bool = True
    lx_embedded_host: str = '127.0.0.1'
    lx_embedded_port: int = 9527
    lx_frontend_password: str = 'musicon-lx-admin'
    lx_admin_user: str = 'musicon'
    lx_admin_password: str = 'musicon'

    model_config = SettingsConfigDict(
        env_file='.env',
        extra='ignore',
        case_sensitive=False,
    )


settings = Settings()

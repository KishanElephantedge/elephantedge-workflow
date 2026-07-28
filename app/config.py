from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Same physical database Synefi's backend uses -- this is the "shared database" half of
    # the shared-DB/siloed-compute pattern. Must point at the same DATABASE_URL as synefi/.env.
    database_url: str
    deepline_cli_path: str = "deepline"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

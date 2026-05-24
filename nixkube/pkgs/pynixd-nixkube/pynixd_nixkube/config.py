from pydantic_settings import BaseSettings, SettingsConfigDict


class NixkubeCentralSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PYNIXD_")

    kube_namespace: str | None = None
    builder_max: int = 3
    builder_min: int = 1
    idle_timeout: int = 300

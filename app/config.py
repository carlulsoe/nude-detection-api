from decimal import Decimal
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    payment_mode: Literal["tempo", "development"] = "tempo"
    mpp_secret_key: SecretStr | None = None
    tempo_recipient: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{40}$")
    tempo_currency: str = Field(
        default="0x20c0000000000000000000000000000000000000",
        pattern=r"^0x[0-9a-fA-F]{40}$",
    )
    tempo_chain_id: Literal[4217, 42431] = 42431
    price_per_image: Decimal = Field(default=Decimal("0.0001"), gt=0, decimal_places=6)
    database_path: str = "data/service.sqlite3"
    dynamodb_table: str | None = None
    max_image_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_image_pixels: int = Field(default=12_000_000, gt=0)
    nsfw_threshold: float = Field(default=0.6, ge=0.2, le=1)

    @field_validator("tempo_chain_id", mode="before")
    @classmethod
    def parse_chain_environment(cls, value):
        return int(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_payment_configuration(self):
        if self.payment_mode == "tempo":
            if not self.tempo_recipient or not self.mpp_secret_key:
                raise ValueError("Tempo requires TEMPO_RECIPIENT and MPP_SECRET_KEY")
            if len(self.mpp_secret_key.get_secret_value()) < 32:
                raise ValueError("MPP_SECRET_KEY must have at least 32 characters")
        return self

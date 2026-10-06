from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    """Requests reject unknown fields so a client typo fails loudly."""

    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel):
    items: list
    total: int
    limit: int
    offset: int

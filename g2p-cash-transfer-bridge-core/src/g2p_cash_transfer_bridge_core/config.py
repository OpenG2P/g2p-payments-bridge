from openg2p_fastapi_common.config import Settings
from pydantic_settings import SettingsConfigDict

from . import __version__


class Settings(Settings):
    model_config = SettingsConfigDict(
        env_prefix="gctb_core_", env_file=".env", extra="allow"
    )

    openapi_title: str = "G2P Cash Transfer Bridge"
    openapi_description: str = """
    This module implements G2P Connect Disburse APIs.
    It contains API layer and multiplexer for different payment backends.

    ***********************************
    Further details goes here
    ***********************************
    """
    openapi_version: str = __version__
    db_dbname: str = "gctbdb"

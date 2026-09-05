"""
salt-pepper — A modern CLI front-end for salt-api.

Rewrite of the 8-year-stale saltstack/pepper.
"""

__all__ = ("__version__", "Pepper", "AsyncPepper", "PepperException")

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__: str | None = version("salt-pepper")
except PackageNotFoundError:
    __version__ = None


from salt_pepper.async_client import AsyncPepper
from salt_pepper.client import Pepper
from salt_pepper.exceptions import PepperException

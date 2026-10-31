"""Shared contract rejection type; supplier retries/fallback must not hide it."""


class DataContractError(ValueError):
    """Invalid data/contract/profile, rather than a supplier transport failure."""

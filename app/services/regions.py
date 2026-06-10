REGION_MARKETS = {
    "cn": {"sh", "sz", "bj", "cn"},
    "us": {"us", "nasdaq", "nyse", "amex"},
}


def region_from_market(market: str | None) -> str:
    if not market:
        return "unknown"
    normalized = market.lower()
    for region, markets in REGION_MARKETS.items():
        if normalized in markets:
            return region
    return "other"


def markets_for_region(region: str | None) -> set[str]:
    if not region or region == "all":
        return set()
    return set(REGION_MARKETS.get(region.lower(), {region.lower()}))

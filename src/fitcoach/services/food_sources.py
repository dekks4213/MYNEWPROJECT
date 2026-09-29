"""External food composition sources behind one small interface.

- Open Food Facts (ODbL; attribution "Open Food Facts" is shown with the source name):
  branded/packaged products.
- USDA FoodData Central (public domain): generic foods from Foundation / SR Legacy data.

Both are optional (FOOD_SOURCES). Failures return no candidates: the diary keeps working.
Only fixed hosts are contacted; responses are size-limited; no redirects are followed.
Status: request/response handling is contract-tested with recorded shapes; live access was
not reachable from the development environment (see docs/STATUS.md).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from fitcoach.domain.food import Basis, Per100, validate_per100
from fitcoach.domain.units import ParseError

log = logging.getLogger(__name__)
MAX_BYTES = 1_000_000
USER_AGENT = "RITM/0.2 (self-hosted nutrition diary)"


@dataclass(frozen=True)
class FoodCandidate:
    source: str
    source_id: str
    name: str
    brand: str | None
    basis: Basis
    per: Per100
    serving_g: Decimal | None = None


class FoodSource(Protocol):
    name: str

    async def search(self, query: str, *, brand: str | None = None) -> list[FoodCandidate]: ...


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _per(values: dict[str, Decimal | None], basis: Basis) -> Per100 | None:
    try:
        per = Per100(**values)
        validate_per100(per, basis)
    except (ValidationError, ParseError):
        return None
    return None if per.energy_kcal is None else per


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> Any:
    response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT})
    if response.status_code != 200 or len(response.content) > MAX_BYTES:
        return None
    return response.json()


class OpenFoodFacts:
    name = "off"
    URL = "https://world.openfoodfacts.org/cgi/search.pl"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def search(self, query: str, *, brand: str | None = None) -> list[FoodCandidate]:
        terms = f"{brand} {query}" if brand else query
        try:
            data = await _get_json(
                self._client,
                self.URL,
                {
                    "search_terms": terms[:100],
                    "search_simple": 1,
                    "json": 1,
                    "page_size": 5,
                    "fields": "code,product_name,brands,nutriments,serving_quantity",
                },
            )
        except (httpx.HTTPError, ValueError):
            log.info("open food facts unavailable")
            return []
        out = []
        for product in (data or {}).get("products", [])[:5]:
            n = product.get("nutriments") or {}
            per = _per(
                {
                    "energy_kcal": _dec(n.get("energy-kcal_100g")),
                    "protein_g": _dec(n.get("proteins_100g")),
                    "fat_g": _dec(n.get("fat_100g")),
                    "carbs_g": _dec(n.get("carbohydrates_100g")),
                    "fiber_g": _dec(n.get("fiber_100g")),
                },
                Basis.PER_100G,
            )
            code, name = str(product.get("code") or ""), product.get("product_name")
            if per is None or not code.isdigit() or not name:
                continue
            out.append(
                FoodCandidate(
                    "off",
                    code[:64],
                    str(name)[:120],
                    (str(product.get("brands") or "").split(",")[0].strip() or None),
                    Basis.PER_100G,
                    per,
                    _dec(product.get("serving_quantity")),
                )
            )
        return out


# FoodData Central nutrient numbers
USDA_NUMBERS = {
    "208": "energy_kcal",
    "203": "protein_g",
    "204": "fat_g",
    "205": "carbs_g",
    "291": "fiber_g",
}


class UsdaFoodData:
    name = "usda"
    URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self._client = client
        self._key = api_key

    async def search(self, query: str, *, brand: str | None = None) -> list[FoodCandidate]:
        if brand:  # generic database: never used for branded products
            return []
        try:
            data = await _get_json(
                self._client,
                self.URL,
                {
                    "query": query[:100],
                    "dataType": "Foundation,SR Legacy",
                    "pageSize": 5,
                    "api_key": self._key,
                },
            )
        except (httpx.HTTPError, ValueError):
            log.info("usda fdc unavailable")
            return []
        out = []
        for food in (data or {}).get("foods", [])[:5]:
            values: dict[str, Decimal | None] = {}
            for nutrient in food.get("foodNutrients", []):
                key = USDA_NUMBERS.get(str(nutrient.get("nutrientNumber")))
                if key and (key != "energy_kcal" or nutrient.get("unitName") == "KCAL"):
                    values[key] = _dec(nutrient.get("value"))
            per = _per(values, Basis.PER_100G)
            fdc_id, name = food.get("fdcId"), food.get("description")
            if per is None or not isinstance(fdc_id, int) or not name:
                continue
            out.append(
                FoodCandidate("usda", str(fdc_id), str(name)[:120], None, Basis.PER_100G, per)
            )
        return out


def build_sources(names: str, *, usda_key: str | None, timeout: float) -> list[FoodSource]:
    wanted = {n.strip() for n in names.split(",") if n.strip()}
    if not wanted:
        return []
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    sources: list[FoodSource] = []
    if "usda" in wanted and usda_key:
        sources.append(UsdaFoodData(client, usda_key))
    if "off" in wanted:
        sources.append(OpenFoodFacts(client))
    return sources

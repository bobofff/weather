"""Settlement station geocoding helpers."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from weather_quant.cache import FileCache
from weather_quant.http import JsonHttpClient


ICAO_PATTERN = re.compile(r"\b[A-Z]{4}\b")
EXPLICIT_ICAO_PATTERNS = (
    re.compile(r"\b(?:ICAO|METAR|STATION\s*ID|STATION\s*CODE)\s*[:#-]?\s*([A-Z]{4})\b"),
    re.compile(r"[\[(]\s*([A-Z]{4})\s*[\])]"),
)
GENERIC_CODE_WORDS = {"INTL", "INTR", "CITY", "MAIN", "WEST", "EAST", "NORTH", "SOUTH"}
KNOWN_STATION_CODES = {
    "esenboga": "LTAC",
    "esenboga international airport": "LTAC",
    "esenboga intl airport": "LTAC",
    "ankara esenboga": "LTAC",
}


class StationLookupError(RuntimeError):
    """Raised when a settlement station cannot be resolved to coordinates."""


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _float_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ascii_text(value: str | None) -> str:
    text = str(value or "").strip()
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", "ignore").decode("ascii")


def _normalize_search_text(value: str | None) -> str:
    text = _ascii_text(value).lower()
    text = re.sub(r"\bintl\.?\b", "international", text)
    text = re.sub(r"\b(?:airport|station|weather|metar)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _valid_icao(value: str | None) -> str | None:
    text = str(value or "").strip().upper()
    if not ICAO_PATTERN.fullmatch(text) or text in GENERIC_CODE_WORDS:
        return None
    return text


def _known_station_code(value: str | None) -> str | None:
    normalized = _normalize_search_text(value)
    if not normalized:
        return None
    for alias, code in KNOWN_STATION_CODES.items():
        if alias == normalized or alias in normalized:
            return code
    return None


def _extract_explicit_icao(value: str | None) -> str | None:
    text = _ascii_text(value).upper()
    for pattern in EXPLICIT_ICAO_PATTERNS:
        match = pattern.search(text)
        if match:
            code = _valid_icao(match.group(1))
            if code:
                return code
    tokens = ICAO_PATTERN.findall(text)
    if tokens and text.rstrip().endswith(tokens[-1]):
        return _valid_icao(tokens[-1])
    return None


def _station_query_variants(query: str) -> tuple[str, ...]:
    candidates = [
        query,
        _ascii_text(query),
        re.sub(r"\bIntl\.?\b", "International", _ascii_text(query), flags=re.IGNORECASE),
        re.sub(r"\bStation\b", "", _ascii_text(query), flags=re.IGNORECASE),
        re.sub(r"\b(?:Intl\.?|International|Airport|Station)\b", " ", _ascii_text(query), flags=re.IGNORECASE),
    ]
    variants: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = " ".join(str(candidate or "").split())
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            variants.append(text)
    return tuple(variants)


def _geojson_coordinates(item: dict[str, Any]) -> tuple[float | None, float | None]:
    geometry = item.get("geometry") if isinstance(item, dict) else None
    if not isinstance(geometry, dict):
        return None, None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        return None, None
    longitude = _float_value(coordinates[0])
    latitude = _float_value(coordinates[1])
    return latitude, longitude


def _aviation_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("features"), list):
        items: list[dict[str, Any]] = []
        for feature in data["features"]:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties")
            if not isinstance(properties, dict):
                continue
            merged = dict(properties)
            merged["geometry"] = feature.get("geometry")
            items.append(merged)
        return items
    if isinstance(data, dict):
        return [data]
    return []


def _open_meteo_items(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return []
    return [item for item in data["results"] if isinstance(item, dict)]


class StationLookupClient:
    """Resolve station IDs or station names to WGS84 coordinates."""

    def __init__(
        self,
        *,
        aviation_base_url: str = "https://aviationweather.gov",
        geocoding_base_url: str = "https://geocoding-api.open-meteo.com",
        http_client: JsonHttpClient | None = None,
        cache: FileCache | None = None,
        cache_max_age_seconds: int = 86_400,
    ) -> None:
        self.aviation_base_url = aviation_base_url.rstrip("/")
        self.geocoding_base_url = geocoding_base_url.rstrip("/")
        self.http = http_client or JsonHttpClient()
        self.cache = cache or FileCache()
        self.cache_max_age_seconds = cache_max_age_seconds

    def lookup(
        self,
        *,
        settlement_station: str | None = None,
        station_id: str | None = None,
        country_code: str | None = None,
        limit: int = 5,
    ) -> tuple[dict[str, Any], ...]:
        query = _optional_text(station_id) or _optional_text(settlement_station)
        if not query:
            raise StationLookupError("Provide settlementStation or stationId.")

        matches: list[dict[str, Any]] = []
        errors: list[str] = []
        icao = (
            _valid_icao(station_id)
            or _extract_explicit_icao(settlement_station)
            or _known_station_code(settlement_station)
        )
        if icao:
            try:
                matches.extend(self._lookup_aviation_station(icao))
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        if settlement_station:
            for query_variant in _station_query_variants(settlement_station):
                try:
                    matches.extend(
                        self._lookup_open_meteo(
                            query_variant,
                            country_code=country_code,
                            limit=limit,
                            station_id=station_id or icao,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
                    continue

        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in matches:
            latitude = _float_value(item.get("latitude"))
            longitude = _float_value(item.get("longitude"))
            if latitude is None or longitude is None:
                continue
            key = (f"{latitude:.5f}", f"{longitude:.5f}")
            if key in seen:
                continue
            seen.add(key)
            item["latitude"] = latitude
            item["longitude"] = longitude
            deduped.append(item)
        if not deduped:
            detail = f" {'; '.join(errors)}" if errors else ""
            raise StationLookupError(f"No station/location found for {query}.{detail}")
        return tuple(deduped[: max(1, limit)])

    def _lookup_aviation_station(self, station_id: str) -> list[dict[str, Any]]:
        params = {"ids": station_id.upper(), "format": "json"}
        data = self._cached_get(
            provider="aviationweather-stationinfo",
            url=f"{self.aviation_base_url}/api/data/stationinfo",
            params=params,
        )
        matches: list[dict[str, Any]] = []
        for item in _aviation_items(data):
            latitude, longitude = _geojson_coordinates(item)
            latitude = latitude if latitude is not None else _float_value(
                item.get("lat") or item.get("latitude")
            )
            longitude = longitude if longitude is not None else _float_value(
                item.get("lon") or item.get("longitude")
            )
            if latitude is None or longitude is None:
                continue
            code = _optional_text(
                item.get("icaoId")
                or item.get("icao")
                or item.get("id")
                or item.get("station_id")
            ) or station_id.upper()
            matches.append(
                {
                    "name": _optional_text(item.get("site") or item.get("name")) or code,
                    "stationId": code,
                    "latitude": latitude,
                    "longitude": longitude,
                    "timezone": _optional_text(item.get("timezone")),
                    "elevation": _float_value(item.get("elev") or item.get("elevation")),
                    "countryCode": _optional_text(item.get("country")),
                    "source": "aviationweather",
                }
            )
        return matches

    def _lookup_open_meteo(
        self,
        query: str,
        *,
        country_code: str | None,
        limit: int,
        station_id: str | None,
    ) -> list[dict[str, Any]]:
        params = {
            "name": query,
            "count": max(1, min(100, limit)),
            "language": "en",
            "format": "json",
        }
        if country_code:
            params["countryCode"] = country_code.upper()
        data = self._cached_get(
            provider="open-meteo-geocoding",
            url=f"{self.geocoding_base_url}/v1/search",
            params=params,
        )
        matches: list[dict[str, Any]] = []
        for item in _open_meteo_items(data):
            latitude = _float_value(item.get("latitude"))
            longitude = _float_value(item.get("longitude"))
            if latitude is None or longitude is None:
                continue
            matches.append(
                {
                    "name": _optional_text(item.get("name")) or query,
                    "stationId": station_id,
                    "latitude": latitude,
                    "longitude": longitude,
                    "timezone": _optional_text(item.get("timezone")),
                    "elevation": _float_value(item.get("elevation")),
                    "countryCode": _optional_text(item.get("country_code")),
                    "country": _optional_text(item.get("country")),
                    "admin1": _optional_text(item.get("admin1")),
                    "source": "open-meteo-geocoding",
                }
            )
        return matches

    def _cached_get(self, *, provider: str, url: str, params: dict[str, Any]) -> Any:
        cache_key = {"provider": provider, "url": url, "params": params}
        data = self.cache.get(cache_key, max_age_seconds=self.cache_max_age_seconds)
        if data is not None:
            return data
        data = self.http.get_json(url, params=params)
        self.cache.set(cache_key, data)
        return data

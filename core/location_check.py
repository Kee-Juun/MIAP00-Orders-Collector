"""Public-IP location preflight for the U.S.-only court site."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


LOCATION_ENDPOINT = "https://ipapi.co/json/"
LOCATION_ENDPOINTS = (
    LOCATION_ENDPOINT,
    "https://get.geojs.io/v1/ip/geo.json",
    "https://ipwho.is/",
)


class LocationVerificationError(RuntimeError):
    """Raised when a run cannot be verified as originating in the U.S."""


class NonUSLocationError(LocationVerificationError):
    """Raised when the public IP is located outside the United States."""


@dataclass(frozen=True)
class PublicLocation:
    country_code: str
    country_name: str
    region: str = ""
    city: str = ""

    @property
    def display_name(self) -> str:
        parts = [self.city, self.region, self.country_name]
        unique_parts: list[str] = []
        for part in parts:
            if part and part.casefold() not in {
                item.casefold() for item in unique_parts
            }:
                unique_parts.append(part)
        return ", ".join(unique_parts) or self.country_code


def verify_us_location(
    *,
    timeout_seconds: float = 8.0,
    endpoint: str | None = None,
) -> PublicLocation:
    """Verify that the current public IP geolocates to the United States.

    This deliberately fails closed. Starting the browser when the lookup is
    unavailable would defeat the purpose of protecting the run from a
    non-U.S. VPN exit location.
    """

    lookup_urls = (endpoint,) if endpoint else LOCATION_ENDPOINTS
    per_lookup_timeout = max(
        1.0, float(timeout_seconds) / max(1, len(lookup_urls))
    )
    payload: Any = None
    last_error: Exception | None = None
    for lookup_url in lookup_urls:
        try:
            response = requests.get(
                lookup_url,
                headers={"Accept": "application/json"},
                timeout=per_lookup_timeout,
            )
            response.raise_for_status()
            candidate: Any = response.json()
            if not isinstance(candidate, dict):
                raise ValueError("location response was not a JSON object")
            if candidate.get("error") or candidate.get("success") is False:
                raise ValueError(
                    str(candidate.get("reason") or candidate.get("message") or "lookup failed")
                )
            payload = candidate
            break
        except (requests.RequestException, ValueError, TypeError) as exc:
            last_error = exc

    if payload is None:
        raise LocationVerificationError(
            "Unable to verify the VPN/public-IP location. The collection was "
            "not started. Check your internet connection, connect the VPN to "
            "any location in the U.S.A., and try again."
        ) from last_error

    country_code = str(
        payload.get("country_code") or payload.get("country") or ""
    ).strip().upper()
    if not country_code:
        raise LocationVerificationError(
            "The VPN/public-IP location service returned no country. The "
            "collection was not started. Connect the VPN to any location in "
            "the U.S.A. and try again."
        )

    location = PublicLocation(
        country_code=country_code,
        country_name=str(payload.get("country_name") or country_code).strip(),
        region=str(payload.get("region") or "").strip(),
        city=str(payload.get("city") or "").strip(),
    )
    if location.country_code != "US":
        raise NonUSLocationError(
            f"Your VPN/public-IP location was detected as {location.display_name}, "
            "not the U.S.A. The collection was not started. Change your VPN "
            "to any state or city in the U.S.A. and try again."
        )
    return location

import logging

import httpx

log = logging.getLogger(__name__)


async def weather(location: str) -> str:
    """Get weather for a location using wttr.in (no API key required)."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"https://wttr.in/{location}",
                params={"format": "j1"},
                headers={"User-Agent": "voice-agent/1.0"},
            )
            data = resp.json()

        current = data["current_condition"][0]
        area = data["nearest_area"][0]
        city = area["areaName"][0]["value"]
        country = area["country"][0]["value"]

        temp_c = current["temp_C"]
        temp_f = current["temp_F"]
        feels_c = current["FeelsLikeC"]
        desc = current["weatherDesc"][0]["value"]
        humidity = current["humidity"]
        wind_kmph = current["windspeedKmph"]

        # Today's high/low
        today = data["weather"][0]
        max_c = today["maxtempC"]
        min_c = today["mintempC"]

        return (
            f"{city}, {country}: {desc}, {temp_c}°C ({temp_f}°F), "
            f"feels like {feels_c}°C. "
            f"Today: high {max_c}°C, low {min_c}°C. "
            f"Humidity {humidity}%, wind {wind_kmph} km/h."
        )
    except Exception as e:
        log.warning(f"Weather failed for '{location}': {e}")
        return f"Weather data unavailable for {location}: {e}"

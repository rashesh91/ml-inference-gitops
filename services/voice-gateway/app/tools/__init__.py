from .calculator import calculator
from .datetime_tool import get_datetime
from .search import search
from .weather import weather

TOOLS = {
    "search": search,
    "weather": weather,
    "calculator": calculator,
    "get_datetime": get_datetime,
}

TOOLS_SCHEMA = """Available tools:
- search(query: str) → Search the web for current news, facts, or information
- weather(location: str) → Get current weather and forecast for a city
- calculator(expression: str) → Evaluate a math expression (e.g. "sqrt(144) + 20 * 3")
- get_datetime() → Get the current date and time"""

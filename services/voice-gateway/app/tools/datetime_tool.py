from datetime import datetime


async def get_datetime() -> str:
    now = datetime.now()
    return now.strftime("Today is %A, %B %d, %Y. The time is %I:%M %p.")

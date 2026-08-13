import httpx
from app.models import CalculationResponse

async def send_callback(callback_url: str, response: CalculationResponse):
    """
    Send calculation result back to .NET API endpoint.
    .NET will then trigger SignalR to notify Angular.
    """
    try:
        async with httpx.AsyncClient(verify=False) as client:
            await client.post(
                callback_url,
                json=response.model_dump(),
                timeout=30.0
            )
    except Exception as e:
        # Log error but don't crash — callback failure should not affect calculation
        print(f"Callback failed to {callback_url}: {str(e)}")
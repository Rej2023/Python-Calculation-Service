from pydantic import BaseModel
from typing import Optional

class CalculationRequest(BaseModel):
    clientCode: str
    entityId: int
    versionId: int
    yearId: int
    timePeriodId: int
    productId: Optional[int] = None
    projectId: Optional[int] = None
    callbackUrl: str
    userId: Optional[str] = None

class CalculationResponse(BaseModel):
    success: bool
    message: str
    clientCode: str
    entityId: int
    versionId: int
    yearId: int
    issueCount: int = 0
    issues: list[str] = []
    # Echoed back from the request so the .NET callback (which has no HTTP
    # context of its own here — this is a server-to-server call from Python,
    # not a user request) knows the exact scope to consolidate and who to
    # attribute it to, without needing to track in-flight requests itself.
    timePeriodId: int = 0
    productId: Optional[int] = None
    projectId: Optional[int] = None
    userId: Optional[str] = None
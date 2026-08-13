from pydantic import BaseModel
from typing import Optional

class CalculationRequest(BaseModel):
    clientCode: str
    entityId: int
    versionId: int
    yearId: int
    productId: Optional[int] = None
    projectId: Optional[int] = None
    callbackUrl: str

class CalculationResponse(BaseModel):
    success: bool
    message: str
    clientCode: str
    entityId: int
    versionId: int
    yearId: int
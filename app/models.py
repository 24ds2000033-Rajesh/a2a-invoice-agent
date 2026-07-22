from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

# Constants & Media Types
A2A_MEDIA_TYPE = "application/a2a+json"
INVOICE_BATCH_TYPE = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSALS_TYPE = "application/vnd.ga5.invoice-action-proposals+json"
RESULTS_TYPE = "application/vnd.ga5.invoice-action-results+json"
RECEIPTS_TYPE = "application/vnd.ga5.invoice-action-receipts+json"

class Part(BaseModel):
    mediaType: str
    data: Dict[str, Any]

class Message(BaseModel):
    messageId: str
    taskId: Optional[str] = None
    contextId: Optional[str] = None
    role: str = "ROLE_USER"
    parts: List[Part]

class Configuration(BaseModel):
    returnImmediately: Optional[bool] = False
    historyLength: Optional[int] = 20
    acceptedOutputModes: Optional[List[str]] = Field(default_factory=list)

class SendMessageRequest(BaseModel):
    message: Message
    configuration: Optional[Configuration] = None

class Task(BaseModel):
    id: str
    contextId: str
    status: str  # e.g., TASK_STATE_INPUT_REQUIRED, TASK_STATE_COMPLETED, TASK_STATE_CANCELED
    history: List[Message]
    artifacts: List[Part] = Field(default_factory=list)

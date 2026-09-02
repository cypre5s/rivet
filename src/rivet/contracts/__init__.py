"""汇总公共契约类型与 JSON Schema 验收清单。"""

from pydantic import BaseModel

from rivet.contracts.common import ErrorDetail, SourceSpan
from rivet.contracts.events import TraceEventEnvelope
from rivet.contracts.guard import (
    AuthorizationDecision,
    CapabilityLease,
    PermissionRequest,
)
from rivet.contracts.ipc import IpcCancel, IpcEvent, IpcRequest, IpcResponse
from rivet.contracts.messages import (
    AssistantMessage,
    ProviderOpaqueState,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from rivet.contracts.modules import ModuleManifest
from rivet.contracts.provider import ModelRequest, ModelResponse, TokenUsage
from rivet.contracts.tools import ToolCall, ToolDefinition
from rivet.contracts.transactions import AcceptanceSpec, PatchSet, TransactionRecord
from rivet.contracts.verification import (
    EvidenceFile,
    EvidenceManifest,
    Verdict,
    VerificationResult,
    VerificationStep,
)

CONTRACT_MODELS: tuple[type[BaseModel], ...] = (
    ErrorDetail,
    SourceSpan,
    TraceEventEnvelope,
    AuthorizationDecision,
    CapabilityLease,
    PermissionRequest,
    IpcCancel,
    IpcEvent,
    IpcRequest,
    IpcResponse,
    AssistantMessage,
    ProviderOpaqueState,
    SystemMessage,
    ToolMessage,
    UserMessage,
    ModuleManifest,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    AcceptanceSpec,
    PatchSet,
    TransactionRecord,
    EvidenceFile,
    EvidenceManifest,
    VerificationResult,
    VerificationStep,
    Verdict,
)

__all__ = ["CONTRACT_MODELS"]

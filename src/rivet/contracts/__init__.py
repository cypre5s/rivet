"""汇总公共契约类型与 JSON Schema 验收清单。"""

from pydantic import BaseModel

from rivet.contracts.common import ArtifactReference, ErrorDetail, SourceSpan
from rivet.contracts.context import ContextBudget, ContextItem, ContextSelection
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
from rivet.contracts.modules import ModuleManifest, ResourceRecord
from rivet.contracts.provider import ModelRequest, ModelResponse, TokenUsage
from rivet.contracts.readers import ReaderRequest, ReaderResult
from rivet.contracts.tools import (
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutput,
    ToolResult,
)
from rivet.contracts.transactions import AcceptanceSpec, PatchSet, TransactionRecord
from rivet.contracts.verification import (
    EvidenceFile,
    EvidenceManifest,
    Verdict,
    VerificationResult,
    VerificationStep,
)

CONTRACT_MODELS: tuple[type[BaseModel], ...] = (
    ArtifactReference,
    ErrorDetail,
    SourceSpan,
    ContextBudget,
    ContextItem,
    ContextSelection,
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
    ResourceRecord,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ReaderRequest,
    ReaderResult,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutput,
    ToolResult,
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

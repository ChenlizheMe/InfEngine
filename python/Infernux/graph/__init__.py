"""Common authored-graph schema, type system, registry and expression IR."""

from .types import CoordinateSpace, PORTABLE_TYPE_SYSTEM, TypeRef, TypeSystem, ValueType
from .registry import (
    COMMON_NODE_REGISTRY,
    NodeDef,
    NodeDefinitionRegistry,
    PortDef,
    PortDirection,
    PortKind,
    PropertyDef,
)
from .common_nodes import COMMON_NODE_DEFINITIONS
from .document import (
    GRAPH_DOCUMENT_SCHEMA,
    GRAPH_DOCUMENT_VERSION,
    GraphDocument,
    GraphDocumentError,
    GraphLinkRecord,
    GraphNodeRecord,
)
from .expression_ir import (
    ExpressionCompileError,
    ExpressionCompiler,
    ExpressionDiagnostic,
    ExpressionInstruction,
    ExpressionOperand,
    ExpressionProgram,
)
from .legacy_adapter import apply_graph_document_to_legacy, graph_document_from_legacy

__all__ = [name for name in globals() if not name.startswith("_")]

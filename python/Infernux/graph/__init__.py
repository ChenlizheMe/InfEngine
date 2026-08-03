"""Common authored-graph schema, type system, registry and expression IR."""

from .types import (
    AssetReference,
    BUILTIN_MESH_NAMES,
    CoordinateSpace,
    PORTABLE_TYPE_SYSTEM,
    TypeRef,
    TypeSystem,
    ValueType,
    builtin_mesh_name,
    builtin_mesh_reference,
)
from .parameters import GraphParameterCollection, GraphParameterDefinition
from .ramp import CURVE_WRAP_MODES, GRADIENT_MODES, MAX_RAMP_KEYS, Curve, CurveKey, Gradient, GradientKey
from .registry import (
    COMMON_NODE_REGISTRY,
    NodeDef,
    NodeDefinitionRegistry,
    PortDef,
    PortDimensionPolicy,
    PortDirection,
    PortKind,
    PropertyDef,
)
from .common_nodes import COMMON_NODE_DEFINITIONS
from .document import (
    GRAPH_DOCUMENT_SCHEMA,
    GraphDocument,
    GraphDocumentError,
    GraphLinkRecord,
    GraphNodeRecord,
    GraphSourceLocation,
)
from .expression_ir import (
    ExpressionCompileError,
    ExpressionCompiler,
    ExpressionDiagnostic,
    ExpressionInstruction,
    ExpressionOperand,
    ExpressionProgram,
)

__all__ = [name for name in globals() if not name.startswith("_")]

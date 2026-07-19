from __future__ import annotations

from typing import Any, Callable


class ValueCodecDescriptor:
    name: str
    can_encode: Callable[[Any], bool]
    can_decode: Callable[[Any], bool]
    encode: Callable[[Any, str, ValueCodecRegistry], Any]
    validate: Callable[[Any, Any, str, ValueCodecRegistry], None]
    decode: Callable[[Any, Any, str, ValueCodecRegistry], Any]

    def __init__(
        self,
        name: str,
        can_encode: Callable[[Any], bool],
        can_decode: Callable[[Any], bool],
        encode: Callable[[Any, str, ValueCodecRegistry], Any],
        validate: Callable[[Any, Any, str, ValueCodecRegistry], None],
        decode: Callable[[Any, Any, str, ValueCodecRegistry], Any],
    ) -> None: ...

    @property
    def identity(self) -> str: ...


class ValueCodecRegistry:
    BUILTIN_CODEC_NAME: str

    def __init__(self) -> None: ...
    @property
    def descriptors(self) -> tuple[ValueCodecDescriptor, ...]: ...
    def register_codec(self, descriptor: ValueCodecDescriptor) -> None: ...
    def encode(self, value: Any, path: str = "value") -> Any: ...
    def validate(self, value: Any, field_meta_or_type: Any, path: str = "value") -> None: ...
    def decode(self, value: Any, field_meta_or_type: Any, path: str = "value") -> Any: ...


VALUE_CODECS: ValueCodecRegistry

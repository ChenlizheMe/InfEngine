from typing import Any

class PanelViewStateField:
    key: str
    attribute: str
    value_type: type | tuple[type, ...]
    default: Any
    def __init__(
        self,
        key: str,
        attribute: str,
        value_type: type | tuple[type, ...],
        default: Any = ...,
    ) -> None: ...
    def capture(self, panel: object) -> Any: ...
    def restore(self, panel: object, value: Any) -> None: ...

class PanelViewStateSchema:
    schema_id: str
    fields: tuple[PanelViewStateField, ...]
    def __init__(
        self,
        schema_id: str,
        fields: tuple[PanelViewStateField, ...] = ...,
    ) -> None: ...
    def capture(self, panel: object) -> dict[str, Any]: ...
    def restore(self, panel: object, data: dict[str, Any]) -> None: ...

class WindowLocator:
    window_id: str
    type_id: str
    def __init__(self, window_id: str, type_id: str) -> None: ...

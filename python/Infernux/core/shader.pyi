"""Type stubs for ShaderInfo resource operations."""

class Shader:
    """Query published shaders and reimport edited ShaderInfo authoring assets."""

    @classmethod
    def is_loaded(cls, name: str, shader_type: str = ...) -> bool:
        """Query GPU publication of a standalone stage or linked material program."""
        ...

    @classmethod
    def reload(cls, shader_id: str, shader_type: str | None = ...) -> bool:
        """Reimport by ShaderInfo Name or asset path; failures raise an exception."""
        ...

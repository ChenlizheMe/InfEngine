"""
Pythonic AudioClip Wrapper

Wraps the C++ AudioClip with a clean Python API for loading, inspecting,
and managing audio clip data.

Usage::

    from Infernux.core.audio_clip import AudioClip

    # Load from file
    clip = AudioClip.load("Assets/Audio/explosion.wav")
    print(clip.duration, clip.sample_rate, clip.channels)

    # Context manager
    with AudioClip.load("Assets/Audio/bgm.wav") as clip:
        source.clip = clip.native
"""

from __future__ import annotations

from typing import Optional

from Infernux import lib as _native_lib

CppAudioClip = getattr(_native_lib, "AudioClip", None)


def _require_native_audio_clip():
    if CppAudioClip is None:
        raise RuntimeError("AudioClip is unavailable in this platform runtime")
    return CppAudioClip


class AudioClip:
    """Pythonic wrapper around C++ AudioClip.

    Provides:
    - Factory method for loading WAV files
    - Read-only properties for clip metadata
    - Context manager for scoped lifecycle
    - Access to native C++ object for component assignment
    """

    def __init__(self, native: CppAudioClip):
        _require_native_audio_clip()
        if native is None:
            raise ValueError("Cannot wrap a None AudioClip")
        self._native = native

    # ==========================================================================
    # Factory Methods
    # ==========================================================================

    @staticmethod
    def load(file_path: str) -> Optional["AudioClip"]:
        """Load an audio clip from a supported audio file.

        Args:
            file_path: Path to a WAV, OGG/Vorbis, MP3, or FLAC file.

        Returns:
            An AudioClip instance, or None if loading failed.
        """
        native = _require_native_audio_clip()()
        if native.load_from_file(file_path):
            return AudioClip(native)
        return None

    @staticmethod
    def from_native(native: CppAudioClip) -> "AudioClip":
        """Wrap an existing C++ AudioClip."""
        return AudioClip(native)

    # ==========================================================================
    # Properties
    # ==========================================================================

    @property
    def native(self) -> CppAudioClip:
        """Access the underlying C++ AudioClip object."""
        return self._native

    @property
    def is_loaded(self) -> bool:
        """Whether the clip has loaded audio data."""
        return self._native.is_loaded

    @property
    def duration(self) -> float:
        """Duration in seconds (Unity: AudioClip.length)."""
        return self._native.duration

    @property
    def sample_count(self) -> int:
        """Total number of sample frames (Unity: AudioClip.samples)."""
        return self._native.sample_count

    @property
    def sample_rate(self) -> int:
        """Sample rate in Hz (Unity: AudioClip.frequency)."""
        return self._native.sample_rate

    @property
    def channels(self) -> int:
        """Number of audio channels (1=mono, 2=stereo)."""
        return self._native.channels

    @property
    def name(self) -> str:
        """Clip name (filename without extension)."""
        return self._native.name

    @property
    def file_path(self) -> str:
        """Source file path."""
        return self._native.file_path

    # ==========================================================================
    # Context Manager
    # ==========================================================================

    def __enter__(self) -> "AudioClip":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.unload()

    def unload(self) -> None:
        """Unload audio data and free memory."""
        if self._native is not None:
            self._native.unload()

    # ==========================================================================
    # Display
    # ==========================================================================

    def __repr__(self) -> str:
        if not self.is_loaded:
            return "<AudioClip (not loaded)>"
        return (
            f"<AudioClip '{self.name}' "
            f"{self.duration:.2f}s "
            f"{self.sample_rate}Hz "
            f"{self.channels}ch>"
        )

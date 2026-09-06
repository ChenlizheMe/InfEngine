"""Standalone utility functions for hierarchy wiring."""
from __future__ import annotations


def _get_py_components(obj):
    return list(obj.get_py_components())


def _get_children(obj):
    return list(obj.get_children())

"""Focused R3-B1 tests for reload-aware callback references."""

from __future__ import annotations

import gc
from datetime import datetime

import pytest

from Infernux.components import InxComponent
from Infernux.components.script_loader import patch_component_class_body
from Infernux.engine.runtime_dispatch import (
    ReloadableCallbackRegistry,
    RuntimeRevisionEpoch,
    current_runtime_epoch,
    publish_runtime_dispatch_epoch,
)
from Infernux.ui.ui_event import UIEvent, UIEvent1


class _MutableTargetRef:
    def __init__(self, target) -> None:
        self.target = target

    def resolve(self):
        return self.target


class _PersistentTargetObject:
    id = 73
    name = "Persistent Target"

    def __init__(self, component) -> None:
        self.component = component

    def get_py_components(self):
        return [self.component]


def _persistent_entry(target_ref, component_name: str, method_name: str):
    from Infernux.ui.ui_event_entry import UIEventEntry

    entry = UIEventEntry(
        component_name=component_name,
        method_name=method_name,
        arguments=[],
    )
    entry.__dict__["target"] = target_ref
    return entry


class _ReloadableCallbackComponent(InxComponent):
    def handle(self, value: int) -> None:
        self.values.append(("old", value))

    def awake(self) -> None:
        self.values = []


class _PlainCallbackOwner:
    def __init__(self) -> None:
        self.values = []

    def handle(self, value: int) -> None:
        self.values.append(value)


def test_inx_component_bound_callback_resolves_the_new_body_after_publication():
    owner = _ReloadableCallbackComponent()
    owner.values = []
    registry = ReloadableCallbackRegistry()
    registry.add_listener(owner.handle)
    registry.invoke(1)
    assert owner.values == [("old", 1)]

    old_method = _ReloadableCallbackComponent.handle

    def replacement(self, value: int) -> None:
        self.values.append(("new", value))

    _ReloadableCallbackComponent.handle = replacement
    publication = publish_runtime_dispatch_epoch((_ReloadableCallbackComponent,))
    publication.commit()
    try:
        registry.invoke(2)
        assert owner.values == [("old", 1), ("new", 2)]
    finally:
        publication.rollback()
        _ReloadableCallbackComponent.handle = old_method


def test_callback_invoke_does_not_reflect_signature_after_registration(monkeypatch):
    owner = _ReloadableCallbackComponent()
    owner.values = []
    registry = ReloadableCallbackRegistry()
    registry.add_listener(owner.handle)
    publication = publish_runtime_dispatch_epoch((_ReloadableCallbackComponent,))
    publication.commit()

    import Infernux.engine.runtime_dispatch as runtime_dispatch

    def unexpected_signature_reflection(_callback):
        raise AssertionError("invoke must not inspect callback signatures")

    try:
        monkeypatch.setattr(runtime_dispatch.inspect, "signature", unexpected_signature_reflection)
        for value in range(3):
            results = registry.invoke(value)
            assert results[0].status == "resolved"
        assert owner.values == [("old", 0), ("old", 1), ("old", 2)]
    finally:
        publication.rollback()


def test_plain_bound_method_is_direct_and_keeps_owner_alive():
    owner = _PlainCallbackOwner()
    registry = ReloadableCallbackRegistry()
    reference = registry.add_listener(owner.handle)
    assert reference.is_direct is True

    del owner
    gc.collect()
    assert registry.listener_count == 1
    registry.invoke(3)
    assert registry.listener_count == 1


def test_duplicate_listener_returns_the_registered_token_and_can_remove_it():
    owner = _ReloadableCallbackComponent()
    registry = ReloadableCallbackRegistry()

    first = registry.add_listener(owner.handle)
    duplicate = registry.add_listener(owner.handle)

    assert duplicate is first
    assert registry.listener_count == 1
    registry.remove_listener(duplicate)
    assert registry.listener_count == 0


def test_reloadable_owner_gc_and_destroy_are_removed_with_explicit_status():
    registry = ReloadableCallbackRegistry()
    owner = _ReloadableCallbackComponent()
    owner.values = []
    registry.add_listener(owner.handle)
    owner._is_destroyed = True
    results = registry.invoke(1)
    assert results[0].status == "owner_invalid"
    assert registry.listener_count == 0

    owner = _ReloadableCallbackComponent()
    registry.add_listener(owner.handle)
    del owner
    gc.collect()
    results = registry.invoke(2)
    assert results[0].status == "owner_unavailable"
    assert registry.listener_count == 0


def test_remove_or_incompatible_signature_rejects_epoch_publication():
    owner = _ReloadableCallbackComponent()
    registry = ReloadableCallbackRegistry()
    registry.add_listener(owner.handle)
    before = current_runtime_epoch()
    old_method = _ReloadableCallbackComponent.handle

    try:
        del _ReloadableCallbackComponent.handle
        with pytest.raises(RuntimeError, match="handle"):
            publish_runtime_dispatch_epoch((_ReloadableCallbackComponent,))
        assert current_runtime_epoch() is before

        def incompatible(self, value: int, extra: int) -> None:
            del extra
            self.values.append(("incompatible", value))

        _ReloadableCallbackComponent.handle = incompatible
        with pytest.raises(RuntimeError, match="signature"):
            publish_runtime_dispatch_epoch((_ReloadableCallbackComponent,))
        assert current_runtime_epoch() is before
    finally:
        _ReloadableCallbackComponent.handle = old_method


def test_lambda_is_direct_and_registry_can_return_diagnostic_results():
    values = []
    registry = ReloadableCallbackRegistry()
    reference = registry.add_listener(lambda value: values.append(value))
    assert reference.is_direct is True
    results = registry.invoke(4)
    assert results[0].status == "direct"
    assert values == [4]


def test_direct_callback_is_independent_of_runtime_epochs():
    values = []
    registry = ReloadableCallbackRegistry()
    reference = registry.add_listener(lambda value: values.append(value))
    newer = RuntimeRevisionEpoch(reference.registration_epoch + 1, {})

    result = registry.invoke(5, epoch=newer)[0]

    assert result.status == "direct"
    assert result.message == ""
    assert values == [5]


def test_direct_body_patch_restores_old_body_when_callback_contract_rejects_epoch():
    class DirectPatchTarget(InxComponent):
        def handle(self, value: int) -> None:
            self.values.append(("old", value))

    class DirectPatchCandidate(InxComponent):
        def handle(self, value: int, extra: int) -> None:
            self.values.append(("new", value, extra))

    DirectPatchCandidate.__name__ = DirectPatchTarget.__name__
    DirectPatchCandidate.__qualname__ = DirectPatchTarget.__qualname__
    owner = DirectPatchTarget()
    owner.values = []
    registry = ReloadableCallbackRegistry()
    registry.add_listener(owner.handle)
    old_method = DirectPatchTarget.handle
    before = current_runtime_epoch()

    with pytest.raises(RuntimeError, match="signature"):
        patch_component_class_body(DirectPatchTarget, DirectPatchCandidate)

    assert DirectPatchTarget.handle is old_method
    assert current_runtime_epoch() is before
    assert registry.invoke(7)[0].status == "resolved"
    assert owner.values == [("old", 7)]


def test_missing_type_in_a_new_epoch_does_not_reuse_previous_descriptor():
    class UnregisteredCallbackOwner(InxComponent):
        def activate(self) -> None:
            self.calls += 1

    owner = UnregisteredCallbackOwner()
    owner.calls = 0
    reference = ReloadableCallbackRegistry().add_listener(owner.activate)
    registration_epoch = current_runtime_epoch()
    assert reference.resolve(registration_epoch).resolved

    newer_epoch = RuntimeRevisionEpoch(registration_epoch.epoch_id + 1, {})
    resolution = reference.resolve(newer_epoch)
    assert resolution.status == "method_missing"


def test_uievent_preserves_callback_exception_propagation():
    event = UIEvent()

    def fail() -> None:
        raise ValueError("callback failed")

    event.add_listener(fail)
    with pytest.raises(ValueError, match="callback failed"):
        event.invoke()

    event1 = UIEvent1()
    event1.add_listener(lambda _value: (_ for _ in ()).throw(ValueError("value failed")))
    with pytest.raises(ValueError, match="value failed"):
        event1.invoke(1)
    assert "listeners=1" in repr(event1)


def test_persistent_uievent_entry_uses_reloaded_body_without_hot_path_reflection(
    monkeypatch,
):
    from Infernux.ui import UIButton

    class PersistentTarget(InxComponent):
        def activate(self) -> None:
            self.calls.append("old")

    owner = PersistentTarget()
    owner.calls = []
    target_ref = _MutableTargetRef(_PersistentTargetObject(owner))
    entry = _persistent_entry(target_ref, "PersistentTarget", "activate")
    button = UIButton()
    button.on_click_entries = [entry]
    button._dispatch_persistent_entries()
    assert owner.calls == ["old"]

    old_method = PersistentTarget.activate

    def replacement(self) -> None:
        self.calls.append("new")

    PersistentTarget.activate = replacement
    publication = publish_runtime_dispatch_epoch((PersistentTarget,))
    publication.commit()
    try:
        import Infernux.engine.runtime_dispatch as runtime_dispatch
        import Infernux.ui.ui_event_entry as ui_event_entry

        def reject_reflection(_callback):
            raise AssertionError("persistent callback invoke reflected its signature")

        monkeypatch.setattr(runtime_dispatch.inspect, "signature", reject_reflection)
        monkeypatch.setattr(ui_event_entry.inspect, "signature", reject_reflection)
        button._dispatch_persistent_entries()
        assert owner.calls == ["old", "new"]
        assert button.debug_dispatch_state()[0]["status"] == "invoked"
    finally:
        publication.rollback()
        PersistentTarget.activate = old_method


def test_persistent_uievent_entry_reuses_binding_across_target_wrappers():
    from Infernux.ui import UIButton

    class StableTarget(InxComponent):
        def activate(self) -> None:
            self.calls += 1

    class RewrappingTargetRef:
        def __init__(self, component) -> None:
            self.component = component

        def resolve(self):
            return _PersistentTargetObject(self.component)

    owner = StableTarget()
    owner.calls = 0
    target_ref = RewrappingTargetRef(owner)
    button = UIButton()
    button.on_click_entries = [
        _persistent_entry(target_ref, "StableTarget", "activate")
    ]

    button._dispatch_persistent_entries()
    first_binding = button._persistent_click_bindings[0]
    button._dispatch_persistent_entries()

    assert button._persistent_click_bindings[0] is first_binding
    assert owner.calls == 2


def test_persistent_uievent_entry_rejects_removed_or_incompatible_method():
    from Infernux.ui import UIButton

    class PersistentContract(InxComponent):
        def activate(self) -> None:
            pass

    owner = PersistentContract()
    target_ref = _MutableTargetRef(_PersistentTargetObject(owner))
    button = UIButton()
    button.on_click_entries = [
        _persistent_entry(target_ref, "PersistentContract", "activate")
    ]
    button._dispatch_persistent_entries()
    before = current_runtime_epoch()
    old_method = PersistentContract.activate

    try:
        del PersistentContract.activate
        with pytest.raises(RuntimeError, match="activate"):
            publish_runtime_dispatch_epoch((PersistentContract,))
        assert current_runtime_epoch() is before

        def incompatible(self, value: int) -> None:
            del value

        PersistentContract.activate = incompatible
        with pytest.raises(RuntimeError, match="signature"):
            publish_runtime_dispatch_epoch((PersistentContract,))
        assert current_runtime_epoch() is before
    finally:
        PersistentContract.activate = old_method


def test_persistent_uievent_entry_reports_destroyed_owner_then_re_resolves_target(
    monkeypatch,
):
    from Infernux.debug import Debug
    from Infernux.ui import UIButton

    class PersistentOwner(InxComponent):
        def activate(self) -> None:
            self.calls += 1

    errors = []
    monkeypatch.setattr(Debug, "log_error", errors.append)
    first = PersistentOwner()
    first.calls = 0
    target_ref = _MutableTargetRef(_PersistentTargetObject(first))
    button = UIButton()
    button.on_click_entries = [
        _persistent_entry(target_ref, "PersistentOwner", "activate")
    ]

    button._dispatch_persistent_entries()
    assert first.calls == 1
    first._is_destroyed = True
    button._dispatch_persistent_entries()
    assert button.debug_dispatch_state()[0]["status"] == "owner_invalid"

    second = PersistentOwner()
    second.calls = 0
    target_ref.target = _PersistentTargetObject(second)
    button._dispatch_persistent_entries()
    assert second.calls == 1
    assert button.debug_dispatch_state()[0]["status"] == "invoked"
    assert errors


def test_persistent_uievent_entry_records_and_logs_user_exception(monkeypatch):
    from Infernux.debug import Debug
    from Infernux.ui import UIButton

    class FailingPersistentOwner(InxComponent):
        def explode(self) -> None:
            raise RuntimeError("persistent callback failed")

    monkeypatch.setattr(Debug, "log_error", lambda _message: None)
    owner = FailingPersistentOwner()
    target_ref = _MutableTargetRef(_PersistentTargetObject(owner))
    button = UIButton()
    button.on_click_entries = [
        _persistent_entry(target_ref, "FailingPersistentOwner", "explode")
    ]

    button._dispatch_persistent_entries()
    assert button.debug_dispatch_state()[0]["status"] == "exception"


def test_persistent_uievent_entry_validation_retires_obsolete_callback_contract():
    from Infernux.ui import UIButton

    class RetiredPersistentOwner(InxComponent):
        def activate(self) -> None:
            pass

    owner = RetiredPersistentOwner()
    target_ref = _MutableTargetRef(_PersistentTargetObject(owner))
    button = UIButton()
    button.on_click_entries = [
        _persistent_entry(target_ref, "RetiredPersistentOwner", "activate")
    ]
    button._dispatch_persistent_entries()
    assert button._persistent_click_bindings[0] is not None

    button.on_click_entries = []
    button.on_validate()
    assert button._persistent_click_bindings == []

    old_method = RetiredPersistentOwner.activate

    def incompatible(self, value: int) -> None:
        del value

    RetiredPersistentOwner.activate = incompatible
    publication = publish_runtime_dispatch_epoch((RetiredPersistentOwner,))
    publication.commit()
    try:
        assert current_runtime_epoch() is publication.after
    finally:
        publication.rollback()
        RetiredPersistentOwner.activate = old_method


def test_debug_console_listener_uses_reloadable_registry_and_propagates(monkeypatch):
    from Infernux.debug import DebugConsole, LogEntry, LogType

    class ConsoleListener(InxComponent):
        def on_log(self, entry) -> None:
            self.messages.append(("old", entry.message))

    previous = DebugConsole._instance
    console = DebugConsole()
    monkeypatch.setattr(console, "_print_entry", lambda _entry: None)
    owner = ConsoleListener()
    owner.messages = []
    console.add_listener(owner.on_log)
    first_entry = LogEntry("first", LogType.LOG, datetime.now())
    console.log(first_entry)
    assert owner.messages == [("old", "first")]

    old_method = ConsoleListener.on_log

    def replacement(self, entry) -> None:
        self.messages.append(("new", entry.message))

    ConsoleListener.on_log = replacement
    publication = publish_runtime_dispatch_epoch((ConsoleListener,))
    publication.commit()
    try:
        console.log(LogEntry("second", LogType.LOG, datetime.now()))
        assert owner.messages[-1] == ("new", "second")

        def fail(_entry) -> None:
            raise ValueError("console listener failed")

        console.add_listener(fail)
        with pytest.raises(ValueError, match="console listener failed"):
            console.log(LogEntry("third", LogType.LOG, datetime.now()))
    finally:
        publication.rollback()
        ConsoleListener.on_log = old_method
        DebugConsole._instance = previous

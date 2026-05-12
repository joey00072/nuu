"""Global keybinding registry.

Ported from Pi's ref/pi/packages/tui/src/keybindings.ts.
"""

from __future__ import annotations


from .keys import KeyId, matches_key

Keybinding = str


class KeybindingDefinition:
    def __init__(self, default_keys: KeyId | list[KeyId], description: str = "") -> None:
        self.default_keys = default_keys
        self.description = description


KeybindingDefinitions = dict[str, KeybindingDefinition]
KeybindingsConfig = dict[str, KeyId | list[KeyId] | None]


TUI_KEYBINDINGS: KeybindingDefinitions = {
    "tui.editor.cursorUp": KeybindingDefinition("up", "Move cursor up"),
    "tui.editor.cursorDown": KeybindingDefinition("down", "Move cursor down"),
    "tui.editor.cursorLeft": KeybindingDefinition(["left", "ctrl+b"], "Move cursor left"),
    "tui.editor.cursorRight": KeybindingDefinition(["right", "ctrl+f"], "Move cursor right"),
    "tui.editor.cursorWordLeft": KeybindingDefinition(["alt+left", "ctrl+left", "alt+b"], "Move cursor word left"),
    "tui.editor.cursorWordRight": KeybindingDefinition(["alt+right", "ctrl+right", "alt+f"], "Move cursor word right"),
    "tui.editor.cursorLineStart": KeybindingDefinition(["home", "ctrl+a"], "Move to line start"),
    "tui.editor.cursorLineEnd": KeybindingDefinition(["end", "ctrl+e"], "Move to line end"),
    "tui.editor.jumpForward": KeybindingDefinition("ctrl+]", "Jump forward to character"),
    "tui.editor.jumpBackward": KeybindingDefinition("ctrl+alt+]", "Jump backward to character"),
    "tui.editor.pageUp": KeybindingDefinition("pageUp", "Page up"),
    "tui.editor.pageDown": KeybindingDefinition("pageDown", "Page down"),
    "tui.editor.deleteCharBackward": KeybindingDefinition("backspace", "Delete character backward"),
    "tui.editor.deleteCharForward": KeybindingDefinition(["delete", "ctrl+d"], "Delete character forward"),
    "tui.editor.deleteWordBackward": KeybindingDefinition(["ctrl+w", "alt+backspace"], "Delete word backward"),
    "tui.editor.deleteWordForward": KeybindingDefinition(["alt+d", "alt+delete"], "Delete word forward"),
    "tui.editor.deleteToLineStart": KeybindingDefinition("ctrl+u", "Delete to line start"),
    "tui.editor.deleteToLineEnd": KeybindingDefinition("ctrl+k", "Delete to line end"),
    "tui.editor.yank": KeybindingDefinition("ctrl+y", "Yank"),
    "tui.editor.yankPop": KeybindingDefinition("alt+y", "Yank pop"),
    "tui.editor.undo": KeybindingDefinition("ctrl+-", "Undo"),
    "tui.input.newLine": KeybindingDefinition(["shift+enter", "ctrl+j", "alt+enter"], "Insert newline"),
    "tui.input.submit": KeybindingDefinition("enter", "Submit input"),
    "tui.input.tab": KeybindingDefinition("tab", "Tab / autocomplete"),
    "tui.input.copy": KeybindingDefinition("ctrl+c", "Copy selection"),
    "tui.select.up": KeybindingDefinition("up", "Move selection up"),
    "tui.select.down": KeybindingDefinition("down", "Move selection down"),
    "tui.select.pageUp": KeybindingDefinition("pageUp", "Selection page up"),
    "tui.select.pageDown": KeybindingDefinition("pageDown", "Selection page down"),
    "tui.select.confirm": KeybindingDefinition("enter", "Confirm selection"),
    "tui.select.cancel": KeybindingDefinition(["escape", "ctrl+c"], "Cancel selection"),
    "app.model.cycleForward": KeybindingDefinition("ctrl+p", "Cycle to next model"),
    "app.model.cycleBackward": KeybindingDefinition("shift+ctrl+p", "Cycle to previous model"),
    "app.models.reorderUp": KeybindingDefinition("alt+up", "Move model up in order"),
    "app.models.reorderDown": KeybindingDefinition("alt+down", "Move model down in order"),
    "app.tool.toggleExpand": KeybindingDefinition("ctrl+o", "Expand/collapse last tool output"),
    "app.thinking.cycle": KeybindingDefinition("shift+tab", "Cycle thinking level"),
    "app.thinking.toggle": KeybindingDefinition("ctrl+t", "Toggle thinking block visibility"),
}


def _normalize_keys(keys: KeyId | list[KeyId] | None) -> list[KeyId]:
    if keys is None:
        return []
    key_list = [keys] if isinstance(keys, str) else keys
    seen: set[str] = set()
    result: list[KeyId] = []
    for k in key_list:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


class KeybindingConflict:
    def __init__(self, key: KeyId, keybindings: list[str]) -> None:
        self.key = key
        self.keybindings = keybindings


class KeybindingsManager:
    def __init__(self, definitions: KeybindingDefinitions, user_bindings: KeybindingsConfig | None = None) -> None:
        self._definitions = definitions
        self._user_bindings: KeybindingsConfig = user_bindings or {}
        self._keys_by_id: dict[str, list[KeyId]] = {}
        self._conflicts: list[KeybindingConflict] = []
        self._rebuild()

    def _rebuild(self) -> None:
        self._keys_by_id.clear()
        self._conflicts.clear()

        user_claims: dict[str, set[str]] = {}
        for keybinding, keys in self._user_bindings.items():
            if keybinding not in self._definitions:
                continue
            for key in _normalize_keys(keys):
                claimants = user_claims.setdefault(key, set())
                claimants.add(keybinding)

        for key, keybindings in user_claims.items():
            if len(keybindings) > 1:
                self._conflicts.append(KeybindingConflict(key, sorted(keybindings)))

        for id_, definition in self._definitions.items():
            user_keys = self._user_bindings.get(id_)
            keys = _normalize_keys(definition.default_keys) if user_keys is None else _normalize_keys(user_keys)
            self._keys_by_id[id_] = keys

    def matches(self, data: str, keybinding: str) -> bool:
        keys = self._keys_by_id.get(keybinding, [])
        for key in keys:
            if matches_key(data, key):
                return True
        return False

    def get_keys(self, keybinding: str) -> list[KeyId]:
        return list(self._keys_by_id.get(keybinding, []))

    def get_definition(self, keybinding: str) -> KeybindingDefinition:
        return self._definitions[keybinding]

    def get_conflicts(self) -> list[KeybindingConflict]:
        return [KeybindingConflict(c.key, list(c.keybindings)) for c in self._conflicts]

    def set_user_bindings(self, user_bindings: KeybindingsConfig) -> None:
        self._user_bindings = user_bindings
        self._rebuild()

    def get_user_bindings(self) -> KeybindingsConfig:
        return dict(self._user_bindings)

    def get_resolved_bindings(self) -> KeybindingsConfig:
        resolved: KeybindingsConfig = {}
        for id_ in self._definitions:
            keys = self._keys_by_id.get(id_, [])
            resolved[id_] = keys[0] if len(keys) == 1 else list(keys)
        return resolved


_global_keybindings: KeybindingsManager | None = None


def set_keybindings(keybindings: KeybindingsManager) -> None:
    global _global_keybindings
    _global_keybindings = keybindings


def get_keybindings() -> KeybindingsManager:
    global _global_keybindings
    if _global_keybindings is None:
        _global_keybindings = KeybindingsManager(TUI_KEYBINDINGS)
    return _global_keybindings

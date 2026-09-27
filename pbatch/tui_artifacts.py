"""Bounded ArtifactRef projection and workspace-scoped read-only preview."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import unquote_to_bytes, urlsplit

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from . import config
from .tui_view_support import clean_text, one_line


ARTIFACT_LIMIT = 64
ARTIFACT_DIAGNOSTIC_LIMIT = 16
ARTIFACT_PREVIEW_MAX_BYTES = 64 * 1024
_PATH_MAX_BYTES = 4096
_LABEL_MAX_BYTES = 512
_DETAIL_MAX_BYTES = 1024


@dataclass(frozen=True)
class ArtifactReference:
    """One explicit ``artifact.created`` reference; never inferred from prose."""

    event_id: str
    seq: int
    run_id: str
    turn_id: str
    actor_id: str
    label: str
    scope: str = "workspace"
    path: str = ""
    uri: str = ""
    name: str = ""
    artifact_id: str = ""
    media_type: str = ""
    digest: str = ""
    declared_size: Optional[int] = None
    reference_error: str = ""


@dataclass(frozen=True)
class ArtifactCatalogSnapshot:
    """Immutable current-turn catalog backed only by canonical events."""

    source: str = ""
    run_id: str = ""
    turn_id: str = ""
    references: tuple[ArtifactReference, ...] = ()
    diagnostics: tuple[str, ...] = ()
    truncated: bool = False
    diagnostics_truncated: bool = False
    event_window_truncated: bool = False


@dataclass(frozen=True)
class ArtifactPreview:
    """Result of one bounded local preview attempt."""

    status: str
    display_path: str = ""
    size_bytes: Optional[int] = None
    content: str = ""
    reason: str = ""
    truncated: bool = False


def parse_artifact_args(args: tuple[str, ...]) -> int:
    """Return a zero-based selection for ``/artifacts [N]``."""
    if not args:
        return 0
    if len(args) != 1:
        raise ValueError("expected at most one 1-based artifact number")
    try:
        selected = int(args[0], 10)
    except ValueError as exc:
        raise ValueError("artifact number must be a positive integer") from exc
    if selected <= 0:
        raise ValueError("artifact number must be a positive integer")
    return selected - 1


def build_artifact_catalog(
        *, events: Iterable[Any], run_id: str, turn_id: str,
        source: str = "runtime events") -> ArtifactCatalogSnapshot:
    """Select current-turn artifact events and retain bounded reference metadata."""
    rows = tuple(events)
    relevant = tuple(event for event in rows
                     if _event_matches(event, run_id, turn_id))
    selected = tuple(event for event in relevant
                     if getattr(event, "kind", "") == "artifact.created")
    kept = selected[-ARTIFACT_LIMIT:]
    references, diagnostics = [], []
    for event in kept:
        reference, warnings = _reference_from_event(event)
        references.append(reference)
        diagnostics.extend(warnings)
    window_cut = bool(turn_id and not any(
        getattr(event, "kind", "") in {"turn.queued", "turn.started"}
        for event in relevant))
    diagnostic_cut = len(diagnostics) > ARTIFACT_DIAGNOSTIC_LIMIT
    return ArtifactCatalogSnapshot(
        source=_display_text(source, _LABEL_MAX_BYTES),
        run_id=_display_text(run_id, _LABEL_MAX_BYTES),
        turn_id=_display_text(turn_id, _LABEL_MAX_BYTES),
        references=tuple(references),
        diagnostics=tuple(diagnostics[-ARTIFACT_DIAGNOSTIC_LIMIT:]),
        truncated=len(selected) > len(kept),
        diagnostics_truncated=diagnostic_cut,
        event_window_truncated=window_cut,
    )


def preview_artifact(
        reference: ArtifactReference, workspace_root: str,
        max_bytes: int = ARTIFACT_PREVIEW_MAX_BYTES) -> ArtifactPreview:
    """Read one UTF-8 regular file without following links outside workspace."""
    if not isinstance(reference, ArtifactReference):
        raise TypeError("reference must be an ArtifactReference")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    local_path, reason = _local_reference(reference)
    if not local_path:
        return ArtifactPreview("metadata_only", reason=reason)
    try:
        root_value = _artifact_root(reference.scope, workspace_root)
        root, relative = _scoped_relative(root_value, local_path)
    except ValueError as exc:
        return ArtifactPreview("blocked", reason=str(exc))
    display = _display_text(relative.as_posix(), _PATH_MAX_BYTES)
    try:
        data, size = _read_scoped_file(root, relative, max_bytes)
    except FileNotFoundError:
        return ArtifactPreview("missing", display, reason="local file is missing")
    except PermissionError:
        return ArtifactPreview("blocked", display, reason="local file is not readable")
    except _ArtifactReadError as exc:
        return ArtifactPreview("blocked", display, reason=str(exc))
    return _text_preview(data, size, display, max_bytes)


def render_artifact_detail(
        catalog: ArtifactCatalogSnapshot, index: Optional[int],
        preview: Optional[ArtifactPreview]) -> str:
    """Render metadata and unformatted preview text for the inspector."""
    if index is None or index < 0 or index >= len(catalog.references):
        return _empty_detail(catalog)
    reference = catalog.references[index]
    current = preview or ArtifactPreview(
        "metadata_only", reason="preview was not requested")
    lines = [
        f"Artifact {index + 1}/{len(catalog.references)}",
        f"Reference: {reference.label}",
        f"Event: {reference.event_id or 'unavailable'}",
        f"Actor: {reference.actor_id or 'unavailable'}",
    ]
    lines.extend(_reference_metadata(reference))
    lines.extend(_preview_metadata(current, reference.scope))
    if current.content:
        lines.extend(["", "--- bounded UTF-8 preview ---", "", current.content])
    return "\n".join(lines)


class TUIArtifactScreen(ModalScreen[None]):
    """Browse explicit artifact references without opening files externally."""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("q", "close", "Close", show=False),
        Binding("r", "refresh", "Refresh", priority=True),
    ]
    CSS = """
    TUIArtifactScreen { align: center middle; background: $background; }
    #artifacts-dialog {
        width: 94%; height: 92%; min-width: 42;
        background: $surface; border: round $accent;
    }
    #artifacts-heading {
        height: 3; padding: 1 2; text-style: bold; background: $boost;
    }
    #artifacts-body { height: 1fr; }
    #artifacts-options {
        width: 34%; min-width: 24; height: 1fr;
        border-right: solid $panel-lighten-1;
    }
    #artifacts-scroll {
        width: 1fr; height: 1fr; padding: 0 2; scrollbar-gutter: stable;
    }
    #artifacts-detail { width: 100%; }
    #artifacts-hint { height: 3; padding: 0 2; color: $text-muted; }
    """

    def __init__(
            self, catalog: ArtifactCatalogSnapshot, workspace_root: str,
            loader: Callable[[], tuple[ArtifactCatalogSnapshot, str]],
            selected: int = 0) -> None:
        super().__init__(id="artifacts-inspector")
        self.inspector_kind = "artifacts"
        self.catalog = catalog
        self.workspace_root = workspace_root
        self._loader = loader
        self._selected = _selected_index(selected, len(catalog.references))
        self._preview = self._load_preview()

    def compose(self) -> ComposeResult:
        with Vertical(id="artifacts-dialog"):
            yield Static(self._heading(), id="artifacts-heading", markup=False)
            with Horizontal(id="artifacts-body"):
                yield OptionList(*self._options(), id="artifacts-options",
                                 markup=False)
                with VerticalScroll(id="artifacts-scroll"):
                    yield Static(self._document(), id="artifacts-detail",
                                 markup=False)
            yield Static(
                "↑/↓ artifact · Enter preview · r refresh · Esc close · "
                "no external open or remote fetch",
                id="artifacts-hint", markup=False)

    def on_mount(self) -> None:
        options = self.query_one("#artifacts-options", OptionList)
        if self._selected is not None:
            options.highlighted = self._selected
        options.focus()

    def on_option_list_option_selected(
            self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self.catalog.references):
            self._selected = event.option_index
            self._preview = self._load_preview()
            self._refresh_document()

    def action_refresh(self) -> None:
        try:
            catalog, root = self._loader()
        except Exception as exc:
            self.notify(f"Unable to refresh artifacts: {exc}", severity="error")
            return
        self.replace_snapshot(catalog, root, force=True)

    def action_close(self) -> None:
        self.dismiss(None)

    def replace_snapshot(
            self, catalog: ArtifactCatalogSnapshot, workspace_root: str, *,
            force: bool = False) -> None:
        """Adopt a newer catalog while preserving the selected event identity."""
        if not isinstance(catalog, ArtifactCatalogSnapshot):
            raise TypeError("catalog must be an ArtifactCatalogSnapshot")
        if not force and catalog == self.catalog and workspace_root == self.workspace_root:
            return
        event_id = self._selected_event_id()
        self.catalog, self.workspace_root = catalog, workspace_root
        self._selected = _find_selection(catalog, event_id, self._selected)
        self._preview = self._load_preview()
        if self.is_mounted:
            options = self.query_one("#artifacts-options", OptionList)
            options.set_options(self._options())
            if self._selected is not None:
                options.highlighted = self._selected
            self._refresh_document()

    def _selected_event_id(self) -> str:
        if self._selected is None or self._selected >= len(self.catalog.references):
            return ""
        return self.catalog.references[self._selected].event_id

    def _load_preview(self) -> Optional[ArtifactPreview]:
        if self._selected is None or self._selected >= len(self.catalog.references):
            return None
        return preview_artifact(
            self.catalog.references[self._selected], self.workspace_root)

    def _refresh_document(self) -> None:
        self.query_one("#artifacts-heading", Static).update(self._heading())
        self.query_one("#artifacts-detail", Static).update(self._document())
        self.query_one("#artifacts-scroll", VerticalScroll).scroll_home(
            animate=False)

    def _heading(self) -> str:
        count = len(self.catalog.references)
        selected = (self._selected + 1) if self._selected is not None else 0
        limits = []
        if self.catalog.truncated:
            limits.append("artifact list truncated")
        if self.catalog.diagnostics_truncated:
            limits.append("diagnostics truncated")
        if self.catalog.event_window_truncated:
            limits.append("event window truncated")
        suffix = f" · {'; '.join(limits)}" if limits else ""
        return f"Artifacts · {selected}/{count} · current turn{suffix}"

    def _document(self) -> str:
        return render_artifact_detail(self.catalog, self._selected, self._preview)

    def _options(self) -> list[Option]:
        return [Option(_option_label(reference, index), id=f"artifact-{index}")
                for index, reference in enumerate(self.catalog.references)]


class _ArtifactReadError(RuntimeError):
    pass


def _event_matches(event: Any, run_id: str, turn_id: str) -> bool:
    if not run_id or not turn_id:
        return False
    return (str(getattr(event, "run_id", "")) == run_id
            and str(getattr(event, "turn_id", "")) == turn_id)


def _reference_from_event(
        event: Any) -> tuple[ArtifactReference, tuple[str, ...]]:
    payload = getattr(event, "payload", {})
    payload = payload if isinstance(payload, Mapping) else {}
    warnings = []
    path = _reference_field(payload, "path", warnings)
    uri = _reference_field(payload, "uri", warnings)
    name = _metadata_field(payload, "name", warnings)
    artifact_id = _metadata_field(payload, "artifact_id", warnings)
    scope = _artifact_scope(payload, warnings)
    label = _display_text(name or path or uri or artifact_id or "unnamed artifact",
                          _LABEL_MAX_BYTES)
    media_type = _metadata_alias(payload, ("media_type", "mime_type"), warnings)
    digest = _metadata_alias(payload, ("sha256", "digest"), warnings)
    declared_size = _declared_size(payload, warnings)
    error = "; ".join(warnings)
    return ArtifactReference(
        event_id=_display_text(getattr(event, "event_id", ""), _LABEL_MAX_BYTES),
        seq=_safe_seq(getattr(event, "seq", 0)),
        run_id=_display_text(getattr(event, "run_id", ""), _LABEL_MAX_BYTES),
        turn_id=_display_text(getattr(event, "turn_id", ""), _LABEL_MAX_BYTES),
        actor_id=_display_text(getattr(event, "actor_id", ""), _LABEL_MAX_BYTES),
        label=label, scope=scope, path=path, uri=uri, name=name,
        artifact_id=artifact_id,
        media_type=media_type, digest=digest, declared_size=declared_size,
        reference_error=error,
    ), tuple(f"{label}: {warning}" for warning in warnings)


def _reference_field(
        payload: Mapping[str, Any], name: str, warnings: list[str]) -> str:
    value = payload.get(name)
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        warnings.append(f"{name} is not a string")
        return ""
    encoded = value.encode("utf-8", "replace")
    if "\0" in value or len(encoded) > _PATH_MAX_BYTES:
        warnings.append(f"{name} is invalid or exceeds {_PATH_MAX_BYTES} bytes")
        return ""
    return value


def _metadata_field(
        payload: Mapping[str, Any], name: str, warnings: list[str]) -> str:
    value = payload.get(name)
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        warnings.append(f"{name} is not a string")
        return ""
    return _display_text(value, _LABEL_MAX_BYTES)


def _metadata_alias(
        payload: Mapping[str, Any], names: tuple[str, ...],
        warnings: list[str]) -> str:
    for name in names:
        if payload.get(name) not in (None, ""):
            return _metadata_field(payload, name, warnings)
    return ""


def _declared_size(
        payload: Mapping[str, Any], warnings: list[str]) -> Optional[int]:
    for name in ("size_bytes", "size"):
        value = payload.get(name)
        if value in (None, ""):
            continue
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        warnings.append(f"{name} is not a non-negative integer")
        return None
    return None


def _artifact_scope(payload: Mapping[str, Any], warnings: list[str]) -> str:
    value = payload.get("scope", "workspace")
    if not isinstance(value, str):
        warnings.append("scope is not a string")
        return "invalid"
    normalized = one_line(clean_text(value)).lower() or "workspace"
    if normalized not in {"workspace", "data"}:
        warnings.append(f"unsupported artifact scope: {normalized}")
        return "invalid"
    return normalized


def _local_reference(reference: ArtifactReference) -> tuple[str, str]:
    if reference.scope not in {"workspace", "data"}:
        return "", "artifact scope is not locally authorized"
    if reference.path:
        return reference.path, ""
    if not reference.uri:
        reason = reference.reference_error or "artifact has no local path reference"
        return "", reason
    try:
        parsed = urlsplit(reference.uri)
    except ValueError:
        return "", "artifact URI is malformed"
    scheme = parsed.scheme.lower()
    if scheme not in {"", "file"}:
        return "", f"{scheme} URI is metadata-only; remote fetch is disabled"
    if parsed.query or parsed.fragment:
        return "", "file URI with query or fragment is metadata-only"
    if parsed.netloc not in {"", "localhost"}:
        return "", "non-local file URI is metadata-only"
    try:
        path = unquote_to_bytes(parsed.path).decode("utf-8")
    except UnicodeDecodeError:
        return "", "file URI path is not valid UTF-8"
    return (path, "") if path else ("", "file URI has no path")


def _artifact_root(scope: str, workspace_root: str) -> str:
    if scope == "workspace":
        return workspace_root
    if scope != "data":
        raise ValueError("artifact scope is not locally authorized")
    if not workspace_root:
        raise ValueError("workspace root is unavailable for data scope")
    return str(config.data_dir(workspace_root))


def _scoped_relative(workspace_root: str, value: str) -> tuple[Path, Path]:
    if not workspace_root:
        raise ValueError("workspace root is unavailable")
    if "\0" in workspace_root or "\0" in value:
        raise ValueError("path contains NUL")
    if len(value.encode("utf-8", "replace")) > _PATH_MAX_BYTES:
        raise ValueError("path exceeds the preview limit")
    try:
        root = Path(workspace_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("workspace root cannot be resolved") from exc
    if not root.is_dir():
        raise ValueError("workspace root is not a directory")
    candidate = Path(value)
    candidate = candidate if candidate.is_absolute() else root / candidate
    lexical = Path(os.path.abspath(os.fspath(candidate)))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact path is outside the current workspace") from exc
    if not relative.parts:
        raise ValueError("artifact path resolves to the workspace directory")
    return root, relative


def _read_scoped_file(root: Path, relative: Path,
                      maximum: int) -> tuple[bytes, int]:
    if (not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_NONBLOCK"))
            or os.open not in os.supports_dir_fd):
        raise _ArtifactReadError("platform cannot guarantee safe no-follow preview")
    opened = []
    common = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    try:
        root_flags = common | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
        opened.append(os.open(root, root_flags))
        for part in relative.parts[:-1]:
            flags = common | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
            opened.append(os.open(part, flags, dir_fd=opened[-1]))
        opened.append(os.open(
            relative.parts[-1], common | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=opened[-1]))
        info = os.fstat(opened[-1])
        if not stat.S_ISREG(info.st_mode):
            raise _ArtifactReadError("artifact is not a regular file")
        return _read_fd(opened[-1], maximum), info.st_size
    except OSError as exc:
        raise _mapped_read_error(exc) from exc
    finally:
        for descriptor in reversed(opened):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _mapped_read_error(error: OSError) -> OSError:
    if error.errno == errno.ENOENT:
        return FileNotFoundError(error.errno, error.strerror)
    if error.errno in {errno.EACCES, errno.EPERM}:
        return PermissionError(error.errno, error.strerror)
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return _ArtifactReadError("symlinks and non-directory path components are blocked")
    return _ArtifactReadError(
        _display_text(error.strerror or "artifact read failed", _DETAIL_MAX_BYTES))


def _read_fd(descriptor: int, maximum: int) -> bytes:
    chunks, remaining = [], maximum + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(16 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _text_preview(data: bytes, size: int, path: str,
                  maximum: int) -> ArtifactPreview:
    truncated = size > maximum or len(data) > maximum
    sample = data[:maximum]
    if b"\0" in sample:
        return ArtifactPreview(
            "binary", path, size_bytes=size,
            reason="binary content is metadata-only", truncated=truncated)
    try:
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        text = decoder.decode(sample, final=not truncated)
    except UnicodeDecodeError:
        return ArtifactPreview(
            "binary", path, size_bytes=size,
            reason="content is not valid UTF-8; preview disabled",
            truncated=truncated)
    return ArtifactPreview(
        "available", path, size_bytes=size,
        content=_safe_content(text),
        reason="preview is a live bounded read" if truncated else "",
        truncated=truncated)


def _safe_content(value: str) -> str:
    text = clean_text(value)
    return "".join(
        character if character in {"\n", "\t"} or not (
            ord(character) < 32 or 127 <= ord(character) <= 159)
        else "�"
        for character in text)


def _reference_metadata(reference: ArtifactReference) -> list[str]:
    rows = [f"Scope: {reference.scope}"]
    if reference.media_type:
        rows.append(f"Declared media type: {reference.media_type}")
    if reference.declared_size is not None:
        rows.append(f"Declared size: {reference.declared_size} bytes")
    if reference.artifact_id:
        rows.append(f"Artifact id: {reference.artifact_id}")
    if reference.digest:
        rows.append(f"Declared digest: {reference.digest}")
    if reference.reference_error:
        rows.append(f"Reference diagnostics: {reference.reference_error}")
    return rows


def _preview_metadata(preview: ArtifactPreview, scope: str) -> list[str]:
    rows = [f"Preview status: {preview.status}"]
    if preview.display_path:
        label = "Data path" if scope == "data" else "Workspace path"
        rows.append(f"{label}: {preview.display_path}")
    if preview.size_bytes is not None:
        rows.append(f"Observed size: {preview.size_bytes} bytes")
    if preview.reason:
        rows.append(f"Note: {preview.reason}")
    if preview.truncated:
        rows.append(f"Preview limit: first {ARTIFACT_PREVIEW_MAX_BYTES} bytes")
    return rows


def _empty_detail(catalog: ArtifactCatalogSnapshot) -> str:
    lines = [
        "No typed artifact.created references are available for the current turn.",
        "Assistant text and command output are not scanned for paths.",
    ]
    if catalog.diagnostics:
        lines.extend(["", "Diagnostics:"])
        lines.extend(f"- {item}" for item in catalog.diagnostics)
    if catalog.event_window_truncated:
        lines.extend(["", "The bounded runtime event window may omit older references."])
    return "\n".join(lines)


def _option_label(reference: ArtifactReference, index: int) -> str:
    media = reference.media_type or "unknown"
    label = _display_text(reference.label, 120)
    return f"{index + 1:>2}  {media[:24]:<24}  {label}"


def _display_text(value: Any, maximum: int) -> str:
    cleaned = clean_text(value)
    safe = "".join(character if not (
        ord(character) < 32 or 127 <= ord(character) <= 159) else "�"
        for character in cleaned)
    text = one_line(safe)
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text
    return encoded[:maximum - 3].decode("utf-8", "ignore") + "…"


def _safe_seq(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _selected_index(value: int, count: int) -> Optional[int]:
    if count <= 0:
        return None
    return min(max(value, 0), count - 1)


def _find_selection(
        catalog: ArtifactCatalogSnapshot, event_id: str,
        fallback: Optional[int]) -> Optional[int]:
    if event_id:
        found = next((index for index, item in enumerate(catalog.references)
                      if item.event_id == event_id), None)
        if found is not None:
            return found
    return _selected_index(fallback or 0, len(catalog.references))

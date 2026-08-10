from __future__ import annotations

import difflib
import hashlib
import html
import json
from pathlib import Path
from typing import Any

from .notebooks import extract_notebook_cells
from .errors import read_diagnostic, redact_message
from .state import task_state
from .workspace import read_manifest


def _lines(value: str) -> list[str]:
    return value.splitlines()


def _unified_table(before: str, after: str) -> str:
    rows: list[str] = []
    matcher = difflib.SequenceMatcher(None, _lines(before), _lines(after), autojunk=False)
    old_line = 1
    new_line = 1
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        old_values = _lines(before)[old_start:old_end]
        new_values = _lines(after)[new_start:new_end]
        if tag == "equal":
            for value in old_values:
                rows.append(
                    f'<tr class="ctx"><td>{old_line}</td><td>{new_line}</td><td> </td><td>{html.escape(value)}</td></tr>'
                )
                old_line += 1
                new_line += 1
        else:
            if tag in {"delete", "replace"}:
                for value in old_values:
                    rows.append(
                        f'<tr class="sub"><td>{old_line}</td><td></td><td>−</td><td>{html.escape(value)}</td></tr>'
                    )
                    old_line += 1
            if tag in {"insert", "replace"}:
                for value in new_values:
                    rows.append(
                        f'<tr class="add"><td></td><td>{new_line}</td><td>+</td><td>{html.escape(value)}</td></tr>'
                    )
                    new_line += 1
    return (
        '<table class="unified"><thead><tr><th>Original</th><th>Propuesta</th>'
        '<th></th><th>Contenido</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def _file_model(path: str, label: str, before: str, after: str, language: str) -> dict[str, Any]:
    before_lines = _lines(before)
    after_lines = _lines(after)
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    added = sum(new_end - new_start for tag, _old_start, _old_end, new_start, new_end in matcher.get_opcodes() if tag in {"insert", "replace"})
    removed = sum(old_end - old_start for tag, old_start, old_end, _new_start, _new_end in matcher.get_opcodes() if tag in {"delete", "replace"})
    return {
        "path": path,
        "label": label,
        "language": language,
        "before": before,
        "after": after,
        "added": added,
        "removed": removed,
        "changed": before != after,
    }


def _notebook_files(before: bytes, after: bytes) -> list[dict[str, Any]]:
    before_cells = {index: (cell_type, language, source) for index, cell_type, language, source in extract_notebook_cells(before)}
    after_cells = {index: (cell_type, language, source) for index, cell_type, language, source in extract_notebook_cells(after)}
    files: list[dict[str, Any]] = []
    for index in sorted(set(before_cells) | set(after_cells)):
        old = before_cells.get(index, ("code", "python", ""))
        new = after_cells.get(index, old)
        language = new[1] if index in after_cells else old[1]
        cell_type = new[0] if index in after_cells else old[0]
        files.append(
            _file_model(
                f"cells/{index:04d}",
                f"Celda {index} · {cell_type} · {language}",
                old[2] if index in before_cells else "",
                new[2] if index in after_cells else "",
                language,
            )
        )
    return files


def _public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    resource = manifest.get("resource") or {}
    return {
        "task_id": manifest.get("task_id"),
        "mode": manifest.get("mode"),
        "workflow_state": manifest.get("workflow_state") or manifest.get("validation_status") or "draft",
        "validation_status": manifest.get("validation_status", "pending"),
        "baseline_sha256": manifest.get("baseline_sha256"),
        "proposed_sha256": manifest.get("proposed_sha256"),
        "approval_digest": manifest.get("approval_digest"),
        "resource": {
            "kind": resource.get("kind"),
            "display_name": resource.get("display_name"),
            "name": resource.get("name"),
            "project": resource.get("project"),
            "location": resource.get("location"),
        },
    }


def _read_sample_receipt(task: Path) -> dict[str, Any] | None:
    """Read only the metadata receipt; never expose sample row payloads."""
    receipt_path = task / "sample-receipt.json"
    if not receipt_path.exists():
        return None
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "errors": ["No se pudo leer el comprobante de muestra"]}
    return value if isinstance(value, dict) else {"ok": False, "errors": ["El comprobante de muestra no es válido"]}


def _public_sample(sample: dict[str, Any] | None, *, current_sha256: str = "") -> dict[str, Any]:
    """Project a sample receipt into safe, row-free review metadata."""
    if not sample:
        return {
            "status": "pending",
            "ok": False,
            "execution_digest": "",
            "limit": None,
            "row_count": 0,
            "columns": [],
            "truncated": False,
            "executed_at": "",
            "errors": [],
        }
    receipt_sha = str(sample.get("content_sha256") or "")
    stale = bool(current_sha256 and receipt_sha and receipt_sha != current_sha256)
    ok = bool(sample.get("ok")) and not stale
    status = "stale" if stale else "ok" if ok else "failed"
    try:
        row_count = max(0, min(int(sample.get("row_count", 0)), 5))
    except (TypeError, ValueError):
        row_count = 0
    raw_limit = sample.get("limit")
    try:
        limit = max(1, min(int(raw_limit), 5)) if isinstance(raw_limit, (int, str)) else None
    except (TypeError, ValueError):
        limit = None
    columns = [str(column)[:128] for column in (sample.get("columns") or []) if str(column).strip()][:25]
    errors = [str(error)[:256] for error in (sample.get("errors") or []) if str(error).strip()][:5]
    if stale:
        errors = ["El contenido cambió después de ejecutar la muestra", *errors][:5]
    return {
        "status": status,
        "ok": ok,
        "execution_digest": str(sample.get("execution_digest") or "")[:128],
        "limit": limit,
        "row_count": row_count,
        "columns": columns,
        "truncated": bool(sample.get("truncated")),
        "executed_at": str(sample.get("executed_at") or "")[:64],
        "errors": errors,
    }


def _workflow_steps(
    state: str,
    validation: dict[str, Any],
    mode: str = "copy",
    resource_kind: str = "",
    sample: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Build the read-only progress rail shown above the diff."""
    status = str(validation.get("status") or state or "draft")
    dry_run = validation.get("dry_run") or {}
    published = state == "published"
    approved = state == "approved"
    validation_done = status == "ready" or approved or published
    validation_error = status in {"blocked_vpc", "blocked_permission", "failed", "changes_required"}
    publication_description = (
        "Commit sobre el repositorio original"
        if mode == "update"
        else "Consulta programada nueva y deshabilitada"
        if resource_kind == "scheduled_query"
        else "Copia nueva en el destino"
    )
    steps = [
        {
            "key": "edit",
            "label": "Edición",
            "status": "done" if state not in {"draft"} else "current",
            "description": "Propuesta aislada en la tarea",
        },
        {
            "key": "validate",
            "label": "Validación",
            "status": "error" if validation_error else ("done" if validation_done else "current"),
            "description": (
                "Prevalidación local"
                if dry_run.get("skipped")
                else "Dry-run en Workbench"
                if validation.get("backend") == "workbench"
                else "Dry-run de BigQuery"
            ),
        },
    ]
    if resource_kind in {"notebook", "shared_query"}:
        sample = sample or {}
        sample_status = str(sample.get("status") or "pending")
        if sample_status == "ok":
            sample_step_status = "done"
            sample_description = f"{sample.get('row_count', 0)} filas · Workbench · solo lectura"
        elif sample_status in {"failed", "stale"}:
            sample_step_status = "error"
            sample_description = "La muestra no es vigente; ejecuta otra con digest aprobado"
        elif validation_error:
            sample_step_status = "locked"
            sample_description = "Disponible después de una validación correcta"
        elif validation_done:
            sample_step_status = "current"
            sample_description = "Opcional · hasta 5 filas dentro de Workbench"
        else:
            sample_step_status = "locked"
            sample_description = "Disponible después del dry-run"
        steps.append(
            {
                "key": "sample",
                "label": "Muestra",
                "status": sample_step_status,
                "description": sample_description,
            }
        )
    steps.extend(
        [
            {
                "key": "approve",
                "label": "Aprobación",
                "status": "done" if approved or published else ("current" if validation_done else "locked"),
                "description": "Digest vigente en la conversación",
            },
            {
                "key": "publish",
                "label": "Publicación",
                "status": "done" if published else "locked",
                "description": publication_description,
            },
        ]
    )
    return steps


def build_review_model(
    manifest: dict[str, Any],
    validation: dict[str, Any],
    before: bytes,
    after: bytes,
    sample: dict[str, Any] | None = None,
    preferences: dict[str, Any] | None = None,
    diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resource = manifest.get("resource") or {}
    if resource.get("kind") == "notebook":
        files = _notebook_files(before, after)
    else:
        filename = str(manifest.get("filename") or "content.sql")
        files = [_file_model(filename, filename, before.decode("utf-8", errors="replace"), after.decode("utf-8", errors="replace"), "sql")]
    changed_files = [file for file in files if file["changed"]]
    summary = {
        "files_changed": len(changed_files),
        "lines_added": sum(file["added"] for file in files),
        "lines_removed": sum(file["removed"] for file in files),
    }
    current_sha256 = hashlib.sha256(after).hexdigest()
    displayed_sample = _public_sample(sample, current_sha256=current_sha256)
    displayed_validation = dict(validation)
    for section_name in ("static", "dry_run"):
        section = displayed_validation.get(section_name)
        if isinstance(section, dict):
            section_copy = dict(section)
            section_copy["errors"] = [redact_message(item) for item in section.get("errors") or []]
            displayed_validation[section_name] = section_copy
    displayed_validation["errors"] = [redact_message(item) for item in validation.get("errors") or []]
    validated_sha256 = validation.get("content_sha256") or manifest.get("proposed_sha256")
    if validated_sha256 and validated_sha256 != current_sha256:
        displayed_validation["stale"] = True
        displayed_validation["publishable"] = False
    displayed_manifest = _public_manifest(manifest)
    displayed_manifest["workflow_state"] = task_state(
        manifest,
        displayed_validation,
        current_sha256=current_sha256,
    )
    displayed_manifest["proposed_sha256"] = current_sha256
    if displayed_validation.get("stale"):
        displayed_manifest["approval_digest"] = None
    model = {
        "manifest": displayed_manifest,
        "validation": displayed_validation,
        "sample": displayed_sample,
        "summary": summary,
        "files": files,
        "workflow_steps": _workflow_steps(
            str(displayed_manifest.get("workflow_state") or "draft"),
            displayed_validation,
            str(displayed_manifest.get("mode") or "copy"),
            str(resource.get("kind") or ""),
            displayed_sample,
        ),
        "preferences": {
            "review_theme": str((preferences or {}).get("review_theme") or "dark"),
            "review_mode": str((preferences or {}).get("review_mode") or "unified"),
            "review_only_changes": bool((preferences or {}).get("review_only_changes", True)),
            "review_context_lines": max(0, min(int((preferences or {}).get("review_context_lines", 3)), 20)),
        },
    }
    if diagnostic:
        model["diagnostic"] = diagnostic
    canonical = json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    model["revision"] = hashlib.sha256(canonical).hexdigest()
    return model


def _validation_cards(validation: dict[str, Any]) -> str:
    status = html.escape("stale" if validation.get("stale") else str(validation.get("status", "pending")))
    method = html.escape(str(validation.get("method", "pending")))
    publishable = "sí" if validation.get("publishable") else "no"
    error_kind = html.escape(str(validation.get("error_kind", "")))
    extra = f'<span class="error-kind">{error_kind}</span>' if error_kind else ""
    return (
        f'<div class="validation-card"><strong>{status}</strong>'
        f'<span>Método: {method}</span><span>Publicable: {publishable}</span>{extra}</div>'
    )


def _file_html(file: dict[str, Any], *, context_lines: int = 3) -> str:
    before = str(file["before"])
    after = str(file["after"])
    split = difflib.HtmlDiff(wrapcolumn=120).make_table(
        _lines(before),
        _lines(after),
        fromdesc="Original",
        todesc="Propuesta",
        context=True,
        numlines=max(0, min(int(context_lines), 20)),
    )
    if not file["changed"]:
        split = '<p class="unchanged">Sin cambios.</p>'
    anchor = "change-" + hashlib.sha256(str(file["path"]).encode("utf-8")).hexdigest()[:12]
    return (
        f'<section class="file-block" id="{anchor}" data-changed="{str(bool(file["changed"])).lower()}">'
        f'<header><div class="file-title"><strong>{html.escape(str(file["label"]))}</strong>'
        f'<small>{html.escape(str(file["path"]))}</small></div>'
        f'<span class="counts" aria-label="{file["added"]} líneas agregadas y {file["removed"]} eliminadas">'
        f'<span class="added">+{file["added"]}</span> '
        f'<span class="removed">−{file["removed"]}</span></span></header>'
        f'<div class="split-view">{split}</div>'
        f'<div class="unified-view">{_unified_table(before, after)}</div>'
        '</section>'
    )


def _format_bytes(value: Any) -> str:
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    scaled = float(amount)
    for unit in units:
        if abs(scaled) < 1024 or unit == units[-1]:
            return f"{scaled:.1f} {unit}" if unit != "B" else f"{amount:,} B"
        scaled /= 1024
    return f"{amount:,} B"


def _render_modern_review(model: dict[str, Any], *, live: bool = False) -> str:
    manifest = model.get("manifest") or {}
    resource = manifest.get("resource") or {}
    summary = model.get("summary") or {}
    validation = model.get("validation") or {}
    sample = _public_sample(model.get("sample") if isinstance(model.get("sample"), dict) else None)
    dry_run = validation.get("dry_run") or {}
    fragments = dry_run.get("fragments") or []
    passed_fragments = sum(1 for fragment in fragments if fragment.get("dry_run_ok"))
    warnings = list(dry_run.get("warnings") or [])
    status = str(manifest.get("workflow_state") or "draft")
    state_class = (
        "error" if status in {"blocked_vpc", "blocked_permission", "failed", "changes_required"}
        else "success" if status in {"ready", "approved", "published"}
        else "current"
    )
    backend = str(validation.get("backend") or "local")
    backend_label = "Workbench" if backend == "workbench" else "Cloud Shell"
    digest = str(manifest.get("approval_digest") or "")
    digest_display = digest[:16] + "…" if len(digest) > 16 else (digest or "pendiente")
    display_name = str(resource.get("display_name") or resource.get("name") or "Recurso")
    location = str(resource.get("location") or "—")
    project = str(resource.get("project") or "—")
    workflow_html = "".join(
        f'<li class="workflow-step {html.escape(str(step["status"]))}">'
        f'<span class="step-dot" aria-hidden="true"></span><div><strong>{html.escape(str(step["label"]))}</strong>'
        f'<small>{html.escape(str(step["description"]))}</small></div></li>'
        for step in model.get("workflow_steps") or []
    )
    nav_html = "".join(
        f'<a class="file-link" href="#change-{hashlib.sha256(str(file["path"]).encode("utf-8")).hexdigest()[:12]}" '
        f'data-changed="{str(bool(file["changed"])).lower()}">'
        f'<span>{html.escape(str(file["label"]))}</span><span class="nav-count">+{file["added"]} −{file["removed"]}</span></a>'
        for file in model.get("files") or []
    )
    files_html = "".join(_file_html(file) for file in model.get("files") or [])
    render_model = dict(model)
    render_model["sample"] = sample
    model_json = json.dumps(render_model, ensure_ascii=False, separators=(",", ":"))
    model_json = model_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    validation_state = html.escape(str(validation.get("status") or status))
    validation_message = (
        "La estimación supera el límite configurado; el dry-run sigue siendo válido."
        if warnings
        else "La validación terminó correctamente."
        if validation.get("ok")
        else "Revisa el diagnóstico y corrige el contenido antes de aprobar."
    )
    fragment_summary = (
        f"{passed_fragments}/{len(fragments)} fragmentos correctos"
        if fragments
        else "consulta SQL"
        if str(resource.get("kind")) in {"shared_query", "scheduled_query"}
        else "sin fragmentos SQL extraíbles"
    )
    digest_button = (
        f'<button class="button quiet" type="button" id="copy-digest" data-digest="{html.escape(digest)}" '
        f'aria-label="Copiar digest de aprobación">Copiar digest</button>'
        if digest
        else ""
    )
    live_script = "" if not live else """
const initialRevision = document.body.dataset.revision;
setInterval(async () => {
  try {
    const response = await fetch("/api/review", {cache: "no-store"});
    const model = await response.json();
    if (model.revision !== initialRevision) window.location.reload();
  } catch (_) {}
}, 2000);
"""
    live_label = "Actualización automática activa" if live else "Snapshot local; usa --serve para actualizar"
    css = """
:root {
  color-scheme: light;
  --bg: #f8fafc; --surface: #ffffff; --surface-subtle: #f1f5f9;
  --text: #0f172a; --muted: #475569; --faint: #64748b; --border: #e2e8f0;
  --primary: #0369a1; --ring: #2563eb; --add-bg: #dcfce7; --add-text: #166534;
  --sub-bg: #fee2e2; --sub-text: #991b1b; --warn-bg: #fef3c7; --warn-text: #92400e;
  --shadow: 0 8px 24px rgba(15, 23, 42, .07); --radius: 12px; --motion: 180ms;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #020617; --surface: #0e1223; --surface-subtle: #1a1e2f;
    --text: #f8fafc; --muted: #cbd5e1; --faint: #94a3b8; --border: #334155;
    --primary: #38bdf8; --ring: #60a5fa; --add-bg: #123322; --add-text: #86efac;
    --sub-bg: #351b22; --sub-text: #fca5a5; --warn-bg: #30230d; --warn-text: #fcd34d;
    --shadow: 0 12px 28px rgba(0, 0, 0, .22);
  }
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
button, input { font: inherit; }
button, a, input { touch-action: manipulation; }
a { color: inherit; }
.skip-link { position: absolute; left: 12px; top: -48px; z-index: 30; padding: 10px 14px; border-radius: 8px; background: var(--primary); color: #fff; font-weight: 700; }
.skip-link:focus { top: 12px; }
.app-header { position: sticky; top: 0; z-index: 20; border-bottom: 1px solid var(--border); background: color-mix(in srgb, var(--surface) 94%, transparent); backdrop-filter: blur(12px); }
.header-inner, main { width: min(1480px, 100%); margin: 0 auto; }
.header-inner { padding: 18px 28px 16px; }
.breadcrumb { color: var(--faint); font-size: .78rem; letter-spacing: .06em; text-transform: uppercase; }
.hero-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-top: 8px; }
h1 { margin: 0; font-size: clamp(1.35rem, 2.6vw, 2rem); letter-spacing: -.035em; }
.resource-meta { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: 5px; color: var(--muted); font-size: .9rem; }
.state-badge, .metric, .button, .filter-label { border: 1px solid var(--border); border-radius: 9px; background: var(--surface); }
.state-badge { display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; color: var(--primary); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .78rem; font-weight: 700; text-transform: uppercase; }
.state-badge.success { color: var(--add-text); } .state-badge.error { color: var(--sub-text); }
.state-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
.summary-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-top: 18px; }
.metric { min-height: 62px; padding: 10px 12px; }
.metric span { display: block; color: var(--faint); font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; }
.metric strong { display: block; margin-top: 2px; font-size: 1rem; }
.workflow { margin-top: 18px; padding: 14px 16px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow); }
.workflow ol { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; margin: 0; padding: 0; list-style: none; }
.workflow-step { position: relative; display: flex; gap: 9px; min-width: 0; padding: 5px 8px; color: var(--faint); }
.workflow-step:not(:last-child)::after { content: ""; position: absolute; top: 12px; left: calc(100% - 3px); width: 10px; border-top: 1px solid var(--border); }
.step-dot { flex: 0 0 auto; width: 14px; height: 14px; margin-top: 3px; border: 2px solid currentColor; border-radius: 50%; }
.workflow-step.done { color: var(--add-text); } .workflow-step.done .step-dot { background: currentColor; }
.workflow-step.current { color: var(--primary); } .workflow-step.error { color: var(--sub-text); }
.workflow-step strong, .workflow-step small { display: block; } .workflow-step strong { font-size: .88rem; } .workflow-step small { margin-top: 2px; font-size: .74rem; }
main { padding: 22px 28px 48px; }
.validation-panel { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; align-items: center; margin-bottom: 18px; padding: 16px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow); }
.validation-panel h2 { margin: 0; font-size: 1rem; } .validation-panel p { margin: 4px 0 0; color: var(--muted); font-size: .9rem; }
.validation-status { color: var(--add-text); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .85rem; font-weight: 700; text-transform: uppercase; }
.validation-status.warning { color: var(--warn-text); }
.sample-inline { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-top: 12px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface-subtle); }
.sample-inline strong, .sample-inline span { display: block; } .sample-inline strong { font-size: .88rem; } .sample-inline span { margin-top: 2px; color: var(--muted); font-size: .82rem; }
.sample-inline code { color: var(--faint); font: .72rem ui-monospace, SFMono-Regular, Consolas, monospace; } .sample-inline.success strong { color: var(--add-text); } .sample-inline.error strong { color: var(--sub-text); } .sample-inline.pending strong { color: var(--primary); }
.validation-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.button { min-height: 42px; padding: 8px 12px; color: var(--text); cursor: pointer; transition: background var(--motion) ease-out, border-color var(--motion) ease-out, transform var(--motion) ease-out; }
.button:hover { border-color: var(--primary); background: var(--surface-subtle); } .button:active { transform: scale(.98); }
.button:focus-visible, .file-link:focus-visible, .filter-label:focus-within { outline: 3px solid color-mix(in srgb, var(--ring) 55%, transparent); outline-offset: 2px; }
.button.primary { border-color: var(--primary); background: var(--primary); color: #fff; } .button.quiet { background: var(--surface-subtle); }
.layout { display: grid; grid-template-columns: 238px minmax(0, 1fr); gap: 16px; align-items: start; }
.sidebar { position: sticky; top: 118px; max-height: calc(100vh - 140px); overflow: auto; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.sidebar-title { padding: 6px 8px 8px; color: var(--faint); font-size: .72rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.file-link { display: flex; justify-content: space-between; gap: 8px; padding: 9px 8px; border-radius: 8px; text-decoration: none; color: var(--muted); font-size: .82rem; }
.file-link:hover { background: var(--surface-subtle); color: var(--text); } .file-link[data-changed="true"] { color: var(--text); font-weight: 650; }
body[data-filter="changed"] .file-link[data-changed="false"] { display: none; }
.nav-count { flex: 0 0 auto; color: var(--faint); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .7rem; }
.review-toolbar { position: sticky; top: 118px; z-index: 10; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; padding: 8px; border: 1px solid var(--border); border-radius: var(--radius); background: color-mix(in srgb, var(--surface) 96%, transparent); }
.segmented { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; } .segmented .button { border: 0; border-radius: 0; }
body[data-mode="split"] [data-mode="split"], body[data-mode="unified"] [data-mode="unified"] { background: var(--primary); color: #fff; }
.filter-label { display: inline-flex; align-items: center; gap: 8px; min-height: 42px; padding: 7px 11px; color: var(--muted); cursor: pointer; }
.filter-label input { accent-color: var(--primary); }
.toolbar-spacer { flex: 1; } .live-note { color: var(--faint); font-size: .78rem; }
.file-block { margin: 12px 0 18px; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow); scroll-margin-top: 180px; }
body[data-filter="changed"] .file-block[data-changed="false"] { display: none; }
.file-block header { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 12px 15px; border-bottom: 1px solid var(--border); background: var(--surface-subtle); }
.file-title strong, .file-title small { display: block; } .file-title small { margin-top: 2px; color: var(--faint); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .72rem; }
.counts { white-space: nowrap; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .82rem; } .added { color: var(--add-text); } .removed { color: var(--sub-text); }
table.diff, table.unified { border-collapse: collapse; width: 100%; table-layout: fixed; font: .84rem/1.55 ui-monospace, SFMono-Regular, Consolas, monospace; }
table.diff td, table.diff th, table.unified td, table.unified th { border: 1px solid var(--border); padding: 4px 9px; vertical-align: top; white-space: pre-wrap; overflow-wrap: anywhere; }
table.diff th, table.unified th { background: var(--surface-subtle); text-align: left; color: var(--muted); font-family: system-ui, sans-serif; font-size: .75rem; }
table.diff .diff_add, table.unified .add { background: var(--add-bg); } table.diff .diff_sub, table.unified .sub { background: var(--sub-bg); } table.diff .diff_chg { background: var(--warn-bg); }
table.unified td:nth-child(1), table.unified td:nth-child(2) { width: 64px; color: var(--faint); text-align: right; user-select: none; }
.unified-view { display: none; overflow-x: auto; } body[data-mode="unified"] .split-view { display: none; } body[data-mode="unified"] .unified-view { display: block; }
body[data-wrap="off"] table.diff td, body[data-wrap="off"] table.unified td { white-space: pre; overflow-wrap: normal; }
.unchanged { padding: 18px; color: var(--faint); } .empty-filter { display: none; padding: 22px; border: 1px dashed var(--border); border-radius: var(--radius); color: var(--muted); text-align: center; }
body[data-filter="changed"] .empty-filter { display: block; } body[data-filter="changed"] .file-block[data-changed="true"] ~ .empty-filter { display: none; }
details { margin-top: 16px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); } summary { padding: 12px 14px; cursor: pointer; color: var(--muted); font-weight: 650; } pre.validation { margin: 0; max-height: 360px; overflow: auto; padding: 14px; border-top: 1px solid var(--border); background: var(--surface-subtle); color: var(--muted); font: .78rem/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
@media (max-width: 900px) { .header-inner, main { padding-left: 16px; padding-right: 16px; } .layout { grid-template-columns: 1fr; } .sidebar { position: static; max-height: none; display: flex; gap: 4px; overflow-x: auto; } .sidebar-title { flex: 0 0 auto; align-self: center; } .file-link { flex: 0 0 auto; } .review-toolbar { top: 100px; } }
@media (max-width: 640px) { .hero-row { display: block; } .state-badge { margin-top: 12px; } .summary-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); } .workflow ol { grid-template-columns: 1fr 1fr; } .workflow-step:not(:last-child)::after { display: none; } .validation-panel { grid-template-columns: 1fr; } .validation-actions { justify-content: flex-start; } .sample-inline { align-items: flex-start; flex-direction: column; } .review-toolbar { top: 100px; } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; } }
"""
    script = """
const taskKey = "queryflow-review-" + (document.body.dataset.task || "task");
const saved = JSON.parse(window.localStorage.getItem(taskKey + "-view") || "null") || {};
const state = {mode: saved.mode || "split", filter: saved.filter || "changed", wrap: saved.wrap || "on"};
const liveRegion = document.getElementById("live-region");
function saveView() { window.localStorage.setItem(taskKey + "-view", JSON.stringify(state)); }
function applyView() {
  document.body.dataset.mode = state.mode;
  document.body.dataset.filter = state.filter;
  document.body.dataset.wrap = state.wrap;
  document.querySelectorAll("[data-mode]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode)));
  document.getElementById("show-only-changes").checked = state.filter === "changed";
  document.getElementById("wrap-lines").checked = state.wrap === "on";
  saveView();
}
document.querySelectorAll("[data-mode]").forEach(button => button.addEventListener("click", () => { state.mode = button.dataset.mode; applyView(); }));
document.getElementById("show-only-changes").addEventListener("change", event => { state.filter = event.target.checked ? "changed" : "all"; applyView(); });
document.getElementById("wrap-lines").addEventListener("change", event => { state.wrap = event.target.checked ? "on" : "off"; applyView(); });
document.getElementById("copy-digest")?.addEventListener("click", async event => {
  const digest = event.currentTarget.dataset.digest;
  try { await navigator.clipboard.writeText(digest); liveRegion.textContent = "Digest copiado."; }
  catch (_) { liveRegion.textContent = "No se pudo copiar el digest; selecciónalo en el encabezado."; }
});
window.addEventListener("beforeunload", () => window.localStorage.setItem(taskKey + "-scroll", String(window.scrollY)));
const savedScroll = window.localStorage.getItem(taskKey + "-scroll");
if (savedScroll) window.scrollTo(0, Number(savedScroll));
applyView();
"""
    validation_json = html.escape(json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True))
    warning_class = " warning" if warnings else ""
    return f"""<!doctype html>
<html lang="es">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>QueryFlow · revisión</title><style>{css}</style></head>
<body data-mode="split" data-filter="changed" data-wrap="on" data-revision="{html.escape(str(model.get('revision', '')))}" data-task="{html.escape(str(manifest.get('task_id', 'task')))}">
<a class="skip-link" href="#review-main">Saltar al diff</a>
<header class="app-header"><div class="header-inner">
  <div class="breadcrumb">QueryFlow / revisión tipo PR</div>
  <div class="hero-row"><div><h1>{html.escape(display_name)}</h1><div class="resource-meta"><span>{html.escape(str(resource.get('kind', '')))}</span><span>{html.escape(project)}</span><span>{html.escape(location)}</span><span>Tarea {html.escape(str(manifest.get('task_id', '')))}</span></div></div><div class="state-badge {state_class}"><span class="state-dot" aria-hidden="true"></span>{html.escape(status)}</div></div>
  <div class="summary-strip"><div class="metric"><span>Cambios</span><strong>{summary.get('files_changed', 0)} archivos/celdas</strong></div><div class="metric"><span>Líneas</span><strong class="added">+{summary.get('lines_added', 0)} <span class="removed">−{summary.get('lines_removed', 0)}</span></strong></div><div class="metric"><span>Backend</span><strong>{html.escape(backend_label)}</strong></div><div class="metric"><span>Digest</span><strong title="{html.escape(digest)}">{html.escape(digest_display)}</strong></div></div>
  <div class="workflow"><ol aria-label="Progreso de la tarea">{workflow_html}</ol></div>
</div></header>
<main id="review-main" tabindex="-1">
  <section class="validation-panel" aria-live="polite"><div><h2>Validación: <span class="validation-status{warning_class}">{validation_state}</span></h2><p>{html.escape(validation_message)} {fragment_summary}; estimación total {_format_bytes(dry_run.get('bytes_processed'))}.</p></div><div class="validation-actions">{digest_button}<span class="live-note">Solo lectura</span></div></section>
  <div class="layout"><aside class="sidebar" aria-label="Navegación de cambios"><div class="sidebar-title">Cambios</div>{nav_html}</aside><section class="diff-area" aria-label="Diff SQL"><div class="review-toolbar"><div class="segmented" role="group" aria-label="Modo de diff"><button class="button" type="button" data-mode="split" aria-pressed="true">Dividida</button><button class="button" type="button" data-mode="unified" aria-pressed="false">Unificada</button></div><label class="filter-label"><input id="show-only-changes" type="checkbox" checked> Mostrar solo cambios</label><label class="filter-label"><input id="wrap-lines" type="checkbox" checked> Ajustar líneas</label><span class="toolbar-spacer"></span><span class="live-note">{html.escape(live_label)}</span></div>{files_html}<div class="empty-filter">No hay cambios visibles con este filtro. Desactiva “Mostrar solo cambios” para ver el contexto completo.</div></section></div>
  <details><summary>Ver validación técnica completa</summary><pre class="validation">{validation_json}</pre></details>
</main>
<div id="live-region" class="sr-only" aria-live="polite"></div><script type="application/json" id="queryflow-model">{model_json}</script><script>{script}{live_script}</script>
</body></html>"""


def _render_minimal_dark_review(model: dict[str, Any], *, live: bool = False) -> str:
    """Render the analyst-facing, dark-first review surface.

    The model remains deliberately richer than this projection so agents and
    integrations can use the workflow metadata without forcing an analyst to
    look at a dashboard full of cards and progress widgets.
    """
    manifest = model.get("manifest") or {}
    resource = manifest.get("resource") or {}
    validation = model.get("validation") or {}
    summary = model.get("summary") or {}
    preferences = model.get("preferences") or {}
    files = list(model.get("files") or [])
    status = str(manifest.get("workflow_state") or validation.get("status") or "draft")
    errors = list(validation.get("errors") or [])
    for section in (validation.get("static") or {}, validation.get("dry_run") or {}):
        errors.extend(str(item) for item in section.get("errors") or [])
    has_error = status in {"blocked_vpc", "blocked_permission", "failed", "changes_required", "conflict"} or bool(errors)
    state_class = "error" if has_error else "success" if status in {"ready", "approved", "published"} else "neutral"
    display_name = str(resource.get("display_name") or resource.get("name") or "Recurso")
    resource_kind = str(resource.get("kind") or "recurso")
    metadata = " · ".join(item for item in (resource_kind, str(resource.get("project") or ""), str(resource.get("location") or "")) if item)
    digest = str(manifest.get("approval_digest") or "")
    digest_button = (
        f'<button class="button subtle" id="copy-digest" type="button" data-digest="{html.escape(digest)}">Copiar digest</button>'
        if digest
        else ""
    )
    diagnostic = model.get("diagnostic") or validation.get("diagnostic")
    diagnostic_payload = diagnostic if isinstance(diagnostic, dict) else {
        "category": validation.get("error_kind"),
        "message": redact_message(errors[0] if errors else "La validación no fue publicable"),
    }
    diagnostic_json = json.dumps(diagnostic_payload, ensure_ascii=False, separators=(",", ":"))
    theme = html.escape(str(preferences.get("review_theme") or "dark"))
    default_mode = html.escape(str(preferences.get("review_mode") or "unified"))
    default_filter = "changed" if preferences.get("review_only_changes", True) else "all"
    context_lines = max(0, min(int(preferences.get("review_context_lines", 3)), 20))
    error_panel = ""
    if has_error:
        error_message = redact_message(errors[0] if errors else "La validación no fue publicable")
        diagnostic_id = str(diagnostic_payload.get("error_id") or "")
        diagnostic_note = f'<p>ID de diagnóstico: <code>{html.escape(diagnostic_id)}</code></p>' if diagnostic_id else ""
        error_panel = (
            f'<section class="error-panel" aria-live="assertive"><div><strong>{html.escape(error_message)}</strong>'
            f'<p>Revisa el diagnóstico antes de volver a validar.</p>{diagnostic_note}</div>'
            f'<button class="button subtle" id="copy-diagnostic" type="button" data-diagnostic="{html.escape(diagnostic_json)}">Copiar diagnóstico</button></section>'
        )
    changed_files = [file for file in files if file.get("changed")]
    selector = ""
    if len(changed_files) > 1:
        options = "".join(
            f'<option value="change-{hashlib.sha256(str(file["path"]).encode("utf-8")).hexdigest()[:12]}">{html.escape(str(file["label"]))}</option>'
            for file in changed_files
        )
        selector = f'<label class="cell-selector">Ir a cambio <select id="change-select">{options}</select></label>'
    files_html = "".join(_file_html(file, context_lines=context_lines) for file in files)
    validation_message = (
        "La validación terminó correctamente."
        if validation.get("ok")
        else "La validación necesita atención."
    )
    content_note = "consulta SQL" if resource_kind in {"shared_query", "scheduled_query"} else "contenido del notebook"
    dry_run = validation.get("dry_run") or {}
    # The HTML already contains the rendered diff. Do not duplicate complete
    # before/after SQL in the embedded model: this keeps large previews small
    # and gives browser integrations metadata without another payload copy.
    public_model = dict(model)
    public_model["files"] = [
        {key: value for key, value in file.items() if key not in {"before", "after"}}
        for file in files
    ]
    model_json = json.dumps(public_model, ensure_ascii=False, separators=(",", ":"))
    model_json = model_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    live_script = "" if not live else """
const initialRevision = document.body.dataset.revision;
setInterval(async () => { try { const response = await fetch('/api/review', {cache: 'no-store'}); const next = await response.json(); if (next.revision !== initialRevision) window.location.reload(); } catch (_) {} }, 2000);
"""
    css = """
:root { color-scheme: dark; --bg:#0f172a; --surface:#172033; --surface-2:#1e293b; --text:#f8fafc; --muted:#a8b3c7; --faint:#71809a; --border:#334155; --blue:#60a5fa; --green:#4ade80; --green-bg:#123c2a; --red:#fb7185; --red-bg:#431d2a; --yellow:#facc15; --radius:10px; }
* { box-sizing:border-box; } html { scroll-behavior:smooth; } body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; } button,select { font:inherit; } a { color:var(--blue); } .skip-link { position:absolute; left:12px; top:-40px; padding:8px 10px; background:var(--blue); color:#07111f; border-radius:6px; z-index:30; } .skip-link:focus { top:12px; }
.topbar { border-bottom:1px solid var(--border); background:#111a2b; } .topbar-inner { max-width:1400px; margin:0 auto; padding:22px clamp(16px,4vw,48px) 18px; } .eyebrow { color:var(--faint); font-size:12px; letter-spacing:.08em; text-transform:uppercase; } .identity { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; margin-top:6px; } h1 { margin:0; font-size:clamp(20px,3vw,30px); letter-spacing:-.02em; } .meta { margin-top:5px; color:var(--muted); font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:12px; overflow-wrap:anywhere; } .state { flex:0 0 auto; padding:5px 9px; border:1px solid var(--border); border-radius:999px; color:var(--muted); font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:12px; } .state.success { border-color:#23864b; color:var(--green); } .state.error { border-color:#a63c57; color:var(--red); } .summary { display:flex; flex-wrap:wrap; gap:10px 18px; margin-top:16px; color:var(--muted); font-size:13px; } .summary strong { color:var(--text); font-family:ui-monospace,SFMono-Regular,Consolas,monospace; } .added { color:var(--green); } .removed { color:var(--red); }
main { max-width:1400px; margin:0 auto; padding:22px clamp(16px,4vw,48px) 56px; } .validation-panel,.error-panel { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px 16px; margin-bottom:14px; border:1px solid var(--border); border-radius:var(--radius); background:var(--surface); } .validation-panel h2 { margin:0; font-size:15px; } .validation-panel p,.error-panel p { margin:3px 0 0; color:var(--muted); font-size:13px; } .validation-status { color:var(--green); font-family:ui-monospace,SFMono-Regular,Consolas,monospace; } .error-panel { border-color:#8d334b; background:var(--red-bg); } .error-panel strong { color:#fecdd3; } .error-panel p { color:#fda4af; }
.toolbar { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-bottom:10px; } .button { min-height:36px; padding:7px 11px; border:1px solid var(--border); border-radius:7px; background:var(--surface-2); color:var(--text); cursor:pointer; } .button:hover { border-color:var(--blue); } .button:focus-visible,select:focus-visible,input:focus-visible { outline:2px solid var(--blue); outline-offset:2px; } .button.active { border-color:var(--blue); background:#244263; } .subtle { color:var(--muted); } .toolbar-spacer { flex:1; } .filter-label,.cell-selector { display:inline-flex; align-items:center; gap:7px; color:var(--muted); } input { accent-color:var(--blue); } select { min-height:34px; padding:5px 8px; border:1px solid var(--border); border-radius:7px; background:var(--surface-2); color:var(--text); }
.file-block { overflow:hidden; margin:0 0 16px; border:1px solid var(--border); border-radius:var(--radius); background:var(--surface); scroll-margin-top:16px; } .file-block header { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:11px 14px; border-bottom:1px solid var(--border); background:var(--surface-2); } .file-title strong,.file-title small { display:block; } .file-title small { margin-top:2px; color:var(--faint); font:12px ui-monospace,SFMono-Regular,Consolas,monospace; } .counts { white-space:nowrap; font:12px ui-monospace,SFMono-Regular,Consolas,monospace; }
table.diff,table.unified { width:100%; border-collapse:collapse; table-layout:fixed; font:13px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace; } table.diff td,table.diff th,table.unified td,table.unified th { border:1px solid var(--border); padding:4px 9px; vertical-align:top; white-space:pre-wrap; overflow-wrap:anywhere; } table.diff th,table.unified th { color:var(--muted); background:var(--surface-2); text-align:left; font:12px system-ui,sans-serif; } table.diff .diff_add,table.unified .add { background:var(--green-bg); } table.diff .diff_sub,table.unified .sub { background:var(--red-bg); } table.diff .diff_chg { background:#4a3b14; } table.unified td:nth-child(1),table.unified td:nth-child(2) { width:64px; color:var(--faint); text-align:right; user-select:none; } .split-view { display:none; overflow:auto; } .unified-view { overflow:auto; } body[data-mode="split"] .split-view { display:block; } body[data-mode="split"] .unified-view { display:none; } body[data-filter="changed"] .file-block[data-changed="false"] { display:none; } .empty-filter { display:none; padding:18px; border:1px dashed var(--border); border-radius:var(--radius); color:var(--muted); text-align:center; } body[data-filter="changed"] .empty-filter { display:block; } body[data-filter="changed"] .file-block[data-changed="true"] ~ .empty-filter { display:none; }
details { margin-top:14px; border:1px solid var(--border); border-radius:var(--radius); background:var(--surface); } summary { padding:11px 14px; cursor:pointer; color:var(--muted); } pre.validation { max-height:360px; overflow:auto; margin:0; padding:14px; border-top:1px solid var(--border); color:var(--muted); background:#111a2b; font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace; } .live-note { color:var(--faint); font-size:12px; } .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
body[data-theme="light"] { --bg:#f8fafc; --surface:#fff; --surface-2:#f1f5f9; --text:#0f172a; --muted:#475569; --faint:#64748b; --border:#cbd5e1; --green-bg:#dcfce7; --red-bg:#fee2e2; } @media (prefers-color-scheme: light) { body[data-theme="system"] { --bg:#f8fafc; --surface:#fff; --surface-2:#f1f5f9; --text:#0f172a; --muted:#475569; --faint:#64748b; --border:#cbd5e1; --green-bg:#dcfce7; --red-bg:#fee2e2; } } @media (max-width:700px) { .identity,.validation-panel,.error-panel { display:block; } .state { display:inline-block; margin-top:10px; } .validation-panel .button,.error-panel .button { margin-top:10px; } .toolbar-spacer { display:none; } table.diff,table.unified { min-width:720px; } }
"""
    script = f"""
const taskKey = "queryflow-review-" + (document.body.dataset.task || "task");
const saved = JSON.parse(localStorage.getItem(taskKey + "-view") || "null") || {{}};
const state = {{mode: saved.mode || document.body.dataset.mode || "{default_mode}", filter: saved.filter || document.body.dataset.filter || "{default_filter}"}};
const liveRegion = document.getElementById("live-region");
function applyView() {{ document.body.dataset.mode = state.mode; document.body.dataset.filter = state.filter; document.querySelectorAll("[data-mode]").forEach(button => button.classList.toggle("active", button.dataset.mode === state.mode)); const only = document.getElementById("show-only-changes"); if (only) only.checked = state.filter === "changed"; localStorage.setItem(taskKey + "-view", JSON.stringify(state)); }}
document.querySelectorAll("[data-mode]").forEach(button => button.addEventListener("click", () => {{ state.mode = button.dataset.mode; applyView(); }}));
document.getElementById("show-only-changes")?.addEventListener("change", event => {{ state.filter = event.target.checked ? "changed" : "all"; applyView(); }});
document.getElementById("copy-digest")?.addEventListener("click", async event => {{ try {{ await navigator.clipboard.writeText(event.currentTarget.dataset.digest); liveRegion.textContent = "Digest copiado."; }} catch (_) {{ liveRegion.textContent = "No se pudo copiar el digest."; }} }});
document.getElementById("copy-diagnostic")?.addEventListener("click", async event => {{ try {{ await navigator.clipboard.writeText(JSON.stringify(JSON.parse(event.currentTarget.dataset.diagnostic), null, 2)); liveRegion.textContent = "Diagnóstico copiado."; }} catch (_) {{ liveRegion.textContent = "No se pudo copiar el diagnóstico."; }} }});
document.getElementById("change-select")?.addEventListener("change", event => document.getElementById(event.target.value)?.scrollIntoView({{behavior:"smooth", block:"start"}}));
applyView();
"""
    validation_json = html.escape(json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True))
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>QueryFlow · revisión</title><style>{css}</style></head>
<body data-theme="{theme}" data-mode="{default_mode}" data-filter="{default_filter}" data-revision="{html.escape(str(model.get('revision', '')))}" data-task="{html.escape(str(manifest.get('task_id', 'task')))}">
<a class="skip-link" href="#review-main">Saltar al diff</a><header class="topbar"><div class="topbar-inner"><div class="eyebrow">QueryFlow · revisión de cambio</div><div class="identity"><div><h1>{html.escape(display_name)}</h1><div class="meta">{html.escape(metadata)} · tarea {html.escape(str(manifest.get('task_id') or ''))}</div></div><span class="state {state_class}">{html.escape(status)}</span></div><div class="summary"><span><strong>+{summary.get('lines_added', 0)}</strong> añadidas</span><span><strong class="removed">−{summary.get('lines_removed', 0)}</strong> eliminadas</span><span>{html.escape('Workbench' if validation.get('backend') == 'workbench' else 'Cloud Shell')}</span></div></div></header>
<main id="review-main" tabindex="-1">{error_panel}<section class="validation-panel" aria-live="polite"><div><h2>Validación · <span class="validation-status">{html.escape(str(validation.get('status') or status))}</span></h2><p>{html.escape(validation_message)} · {html.escape(content_note)} · estimación {html.escape(_format_bytes(dry_run.get('bytes_processed')))}.</p></div><div>{digest_button}</div></section><div class="toolbar"><div><button class="button" type="button" data-mode="unified" aria-pressed="true">Unificada</button><button class="button" type="button" data-mode="split" aria-pressed="false">Dividida</button></div><label class="filter-label" aria-label="Mostrar solo cambios"><input id="show-only-changes" type="checkbox" {'checked' if default_filter == 'changed' else ''}> Solo cambios</label>{selector}<span class="toolbar-spacer"></span><span class="live-note">{'Actualización automática activa' if live else 'Vista local · solo lectura'}</span></div>{files_html}<div class="empty-filter">No hay cambios visibles con este filtro.</div><details><summary>Ver detalles técnicos</summary><pre class="validation">{validation_json}</pre></details></main><div id="live-region" class="sr-only" aria-live="polite"></div><script type="application/json" id="queryflow-model">{model_json}</script><script>{script}{live_script}</script></body></html>"""


def render_review_model(model: dict[str, Any], *, live: bool = False) -> str:
    return _render_minimal_dark_review(model, live=live)


def render_review_html(before: str, after: str, validation: dict[str, Any]) -> str:
    """Backward-compatible renderer for a single SQL document."""
    manifest = {
        "task_id": "review",
        "mode": "copy",
        "workflow_state": validation.get("status", "pending"),
        "resource": {"kind": "shared_query", "display_name": "SQL", "location": ""},
    }
    model = build_review_model(manifest, validation, before.encode("utf-8"), after.encode("utf-8"))
    return render_review_model(model)


def write_review_html(task: Path, before: bytes | str, after: bytes | str, validation: dict[str, Any], preferences: dict[str, Any] | None = None) -> Path:
    manifest = read_manifest(task)
    before_bytes = before.encode("utf-8") if isinstance(before, str) else before
    after_bytes = after.encode("utf-8") if isinstance(after, str) else after
    target = task / "review.html"
    diagnostic = read_diagnostic(task)
    target.write_text(
        render_review_model(build_review_model(manifest, validation, before_bytes, after_bytes, _read_sample_receipt(task), preferences, diagnostic.to_dict() if diagnostic else None)),
        encoding="utf-8",
    )
    return target

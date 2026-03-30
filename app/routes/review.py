import html
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.constants import REVOCATION_ID_PATTERN, REVOKE_JOB_ID_PATTERN
from app.routes.status import _public_job
from app.services.auth import get_current_user
from app.services import bookstack as bs
from app.services import audit, storage
from app.services import revocation_processor
from app.templates import templates

settings = get_settings()
router = APIRouter(prefix="/review", tags=["review"])


@router.get("", response_class=HTMLResponse)
def review_page(request: Request, user=Depends(get_current_user)):
    overview = bs.get_all_books_overview()
    role = user.get("role")
    forbidden = {settings.bookstack_staging_shelf_id, settings.bookstack_revoked_shelf_id}
    shelves = [s for s in overview["shelves"] if s["id"] not in forbidden]
    return templates.TemplateResponse("review.html", {
        "request": request,
        "user": user,
        "drafts": overview["drafts"],
        "published_books": overview["published"],
        "revoked_books": storage.get_revoked_registry(),
        "shelves": shelves,
        "is_reviewer": role in ("revisor", "admin"),
        "is_admin": role == "admin",
        "bookstack_url": settings.bookstack_base_url,
    })


@router.post("/{book_id}/publish", response_class=HTMLResponse)
def publish_book(
    book_id: int,
    request: Request,
    user=Depends(get_current_user),
    shelf_id: int = Form(...),
):
    if user.get("role") not in ("revisor", "admin"):
        raise HTTPException(403, "Acesso restrito a revisores.")

    _validate_destination_shelf(shelf_id)

    drafts = bs.get_draft_books()
    draft = next((d for d in drafts if d["book_id"] == book_id), None)
    if draft is None:
        raise HTTPException(404, "Rascunho não encontrado.")
    title = draft["title"]
    book_url = draft["bookstack_url"]

    bs.publish_book(book_id, shelf_id)
    audit.log(user["email"], "publicar", title)

    t = html.escape(title)
    u = html.escape(book_url)
    bid = html.escape(str(book_id))
    return HTMLResponse(f"""
<tbody id="draft-rows-{bid}" style="background:#F0FAF1;">
  <tr>
    <td colspan="5">
      <div class="d-flex align-items-center justify-content-between flex-wrap px-2 py-2" style="gap:.5rem;">
        <div class="d-flex align-items-center">
          <i class="fas fa-check-circle mr-2" style="color:#168821;" aria-hidden="true"></i>
          <strong style="color:#168821;">{t}</strong>
          <span class="text-down-01 ml-2" style="color:#168821;">— publicado com sucesso</span>
        </div>
        <a href="{u}" target="_blank" rel="noopener noreferrer" class="br-button secondary small">
          <i class="fas fa-external-link-alt mr-1" aria-hidden="true"></i> Ver publicação
        </a>
      </div>
    </td>
  </tr>
</tbody>
<div id="action-toast" hx-swap-oob="true">
  <div class="br-message success action-toast" role="status">
    <div class="icon"><i class="fas fa-check-circle" aria-hidden="true"></i></div>
    <div class="content">
      <p class="text-medium-weight mb-1">Normativo publicado com sucesso.</p>
      <p class="text-down-01 mb-0">Recarregue a página para ver na lista de publicados.</p>
    </div>
  </div>
</div>""")


@router.post("/{book_id}/move", response_class=HTMLResponse)
def move_book_route(
    book_id: int,
    request: Request,
    user=Depends(get_current_user),
    shelf_id: int = Form(...),
):
    if user.get("role") not in ("revisor", "admin"):
        raise HTTPException(403, "Acesso restrito a revisores.")

    _validate_destination_shelf(shelf_id)

    title = bs.get_published_book_title(book_id)
    if title is None:
        raise HTTPException(404, "Normativo publicado não encontrado.")

    bs.move_book(book_id, shelf_id)
    audit.log(user["email"], "mover", title)

    return HTMLResponse(content="", status_code=200, headers={"HX-Refresh": "true"})


@router.delete("/{book_id}", response_class=HTMLResponse)
def delete_book_route(book_id: int, request: Request, user=Depends(get_current_user)):
    role = user.get("role")
    if role not in ("revisor", "admin"):
        raise HTTPException(403, "Acesso restrito a revisores e administradores.")

    drafts = bs.get_draft_books()
    draft = next((d for d in drafts if d["book_id"] == book_id), None)
    title = draft["title"] if draft else str(book_id)

    if role != "admin":
        if draft is None or draft.get("uploaded_by") != user["email"]:
            raise HTTPException(403, "Você só pode remover rascunhos que você mesmo enviou.")

    bs.delete_book(book_id)
    audit.log(user["email"], "remover", title)

    return HTMLResponse("")


def _validate_destination_shelf(shelf_id: int) -> None:
    """Valida que shelf_id existe e não é staging nem revogadas."""
    forbidden = {settings.bookstack_staging_shelf_id, settings.bookstack_revoked_shelf_id}
    available = {s["id"] for s in bs.get_shelves()}
    if shelf_id in forbidden or shelf_id not in available:
        raise HTTPException(400, "Prateleira de destino inválida.")


def _render_revoke_progress(request, job: dict):
    return templates.TemplateResponse(
        "partials/revoke_progress.html",
        {"request": request, "job": _public_job(job)},
    )


def _load_and_authorize_revoke_job(job_id: str, user: dict) -> dict:
    """Carrega o job de revogação e verifica acesso (owner ou revisor/admin)."""
    job = storage.load_status(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado.")
    owner = job.get("owner")
    if user.get("role") not in ("revisor", "admin") and (not owner or owner != user["email"]):
        raise HTTPException(403, "Acesso negado.")
    return job


@router.get("/revoke-status/{job_id}", response_class=HTMLResponse)
def revoke_status(
    request: Request,
    job_id: str = Path(..., pattern=REVOKE_JOB_ID_PATTERN),
    user=Depends(get_current_user),
):
    job = _load_and_authorize_revoke_job(job_id, user)
    return _render_revoke_progress(request, job)


@router.post("/revoke-cancel/{job_id}", response_class=HTMLResponse)
def cancel_revoke_job(
    request: Request,
    job_id: str = Path(..., pattern=REVOKE_JOB_ID_PATTERN),
    user=Depends(get_current_user),
):
    job = _load_and_authorize_revoke_job(job_id, user)
    if job.get("status") == "processing":
        job = {**job, "status": "cancelled"}
        storage.save_status(job_id, job)
    return _render_revoke_progress(request, job)


@router.post("/{book_id}/invalidate", response_class=HTMLResponse)
def invalidate_book_route(book_id: int, request: Request, user=Depends(get_current_user)):
    if user.get("role") not in ("revisor", "admin"):
        raise HTTPException(403, "Acesso restrito a revisores.")

    job_id = f"rev_{secrets.token_urlsafe(12)}"
    storage.save_status(job_id, {
        "id": job_id,
        "status": "processing",
        "current_step": 1,
        "total_steps": 5,
        "current_step_label": "Iniciando...",
        "progress_pct": 0,
        "owner": user["email"],
    })
    if not revocation_processor.run_in_background(job_id, book_id, user["email"]):
        storage.save_status(job_id, {
            "id": job_id,
            "status": "error",
            "error": "Servidor ocupado. Aguarde e tente novamente.",
            "current_step": 0,
            "total_steps": 5,
            "current_step_label": "Erro",
            "progress_pct": 0,
            "owner": user["email"],
        })
        bid = html.escape(str(book_id))
        return HTMLResponse(
            f'<tbody id="pub-rows-{bid}"></tbody>'
            '<div id="action-toast" hx-swap-oob="true">'
            '<div class="br-message danger action-toast" role="status">'
            '<div class="icon"><i class="fas fa-times-circle" aria-hidden="true"></i></div>'
            '<div class="content">'
            '<p class="text-medium-weight mb-1">Servidor ocupado.</p>'
            '<p class="text-down-01 mb-0">O número máximo de revogações simultâneas foi atingido. Tente novamente em instantes.</p>'
            '</div></div></div>'
        )

    job = storage.load_status(job_id)
    inner = templates.env.get_template("partials/revoke_progress.html").render(job=job)
    bid = html.escape(str(book_id))
    toast = (
        '<div id="action-toast" hx-swap-oob="true">'
        '<div class="br-message warning action-toast" role="status">'
        '<div class="icon"><i class="fas fa-ban" aria-hidden="true"></i></div>'
        '<div class="content">'
        '<p class="text-medium-weight mb-1">Revogação iniciada.</p>'
        '<p class="text-down-01 mb-0">Acompanhe o progresso na linha do normativo.</p>'
        '</div></div></div>'
    )
    return HTMLResponse(
        f'<tbody id="pub-rows-{bid}"><tr>'
        f'<td colspan="4" style="padding:1rem;">{inner}</td>'
        f'</tr></tbody>'
        + toast
    )




@router.delete("/revoked/{revocation_id}", response_class=HTMLResponse)
def delete_revoked(
    request: Request,
    revocation_id: str = Path(..., pattern=REVOCATION_ID_PATTERN),
    user=Depends(get_current_user),
):
    if user.get("role") != "admin":
        raise HTTPException(403, "Acesso restrito a administradores.")

    entry = storage.remove_from_revoked_registry(revocation_id)
    if entry:
        if entry.get("bookstack_book_id"):
            bs.delete_book_from_bookstack(entry["bookstack_book_id"])
        if entry.get("pdf_key"):
            storage.delete_pdf(entry["pdf_key"])
            job_id = entry["pdf_key"].removeprefix("pdfs/").removesuffix(".pdf")
            storage.unregister_pdf_checksum_by_job_id(job_id)

    audit.log(user["email"], "remover_revogado", entry.get("title", revocation_id) if entry else revocation_id)
    return HTMLResponse("")

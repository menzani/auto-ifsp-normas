import html as html_module
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.services.auth import get_current_user
from app.services import bookstack as bs
from app.services import audit, storage
from app.services import revocation_processor
from app.templates import templates

settings = get_settings()
router = APIRouter(prefix="/review", tags=["review"])


@router.get("", response_class=HTMLResponse)
async def review_page(request: Request, user=Depends(get_current_user)):
    overview = bs.get_all_books_overview()
    role = user.get("role")
    shelves = [s for s in overview["shelves"] if s["id"] != settings.bookstack_staging_shelf_id]
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
async def publish_book(
    book_id: int,
    request: Request,
    user=Depends(get_current_user),
    shelf_id: int = Form(...),
):
    if user.get("role") not in ("revisor", "admin"):
        raise HTTPException(403, "Acesso restrito a revisores.")

    drafts = bs.get_draft_books()
    draft = next((d for d in drafts if d["book_id"] == book_id), None)
    title = draft["title"] if draft else str(book_id)
    book_url = draft["bookstack_url"] if draft else settings.bookstack_base_url

    bs.publish_book(book_id, shelf_id)
    audit.log(user["email"], "publicar", title)

    t = html_module.escape(title)
    u = html_module.escape(book_url)
    return HTMLResponse(f"""
<tbody id="draft-rows-{book_id}" style="background:#F0FAF1;">
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
</tbody>""")


@router.delete("/{book_id}", response_class=HTMLResponse)
async def delete_book_route(book_id: int, request: Request, user=Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Acesso restrito a administradores.")

    drafts = bs.get_draft_books()
    title = next((d["title"] for d in drafts if d["book_id"] == book_id), str(book_id))

    bs.delete_book(book_id)
    audit.log(user["email"], "remover", title)

    return HTMLResponse("")


@router.get("/revoke-status/{job_id}", response_class=HTMLResponse)
async def revoke_status(job_id: str, request: Request, user=Depends(get_current_user)):
    job = storage.load_status(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado.")
    return templates.TemplateResponse(
        "partials/revoke_progress.html",
        {"request": request, "job": job},
    )


@router.post("/{book_id}/invalidate", response_class=HTMLResponse)
async def invalidate_book_route(book_id: int, request: Request, user=Depends(get_current_user)):
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
    })
    revocation_processor.run_in_background(job_id, book_id, user["email"])

    job = storage.load_status(job_id)
    inner = templates.env.get_template("partials/revoke_progress.html").render(job=job)
    bid = html_module.escape(str(book_id))
    return HTMLResponse(
        f'<tbody id="pub-rows-{bid}"><tr>'
        f'<td colspan="4" style="padding:1rem;">{inner}</td>'
        f'</tr></tbody>'
    )


@router.delete("/revoked/{revocation_id}", response_class=HTMLResponse)
async def delete_revoked(revocation_id: str, request: Request, user=Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Acesso restrito a administradores.")

    entry = storage.remove_from_revoked_registry(revocation_id)
    if entry:
        if entry.get("bookstack_book_id"):
            bs.delete_book_from_bookstack(entry["bookstack_book_id"])
        if entry.get("pdf_key"):
            storage.delete_pdf(entry["pdf_key"])

    audit.log(user["email"], "remover_revogado", revocation_id)
    return HTMLResponse("")

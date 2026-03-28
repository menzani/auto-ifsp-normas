from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.services.auth import require_admin
from app.services import users as user_store
from app.services import audit
from app.templates import templates

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_class=HTMLResponse)
async def admin_users(request: Request, user=Depends(require_admin)):
    all_users = user_store.list_users()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "all_users": all_users,
        "valid_roles": user_store.VALID_ROLES,
    })


@router.post("/users/{email:path}/role", response_class=HTMLResponse)
async def update_role(
    email: str,
    request: Request,
    role: str = Form(...),
    user=Depends(require_admin),
):
    user_store.set_role(email, role)
    audit.log(user["email"], "alterar_papel", f"{email} → {role}")

    all_users = user_store.list_users()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "all_users": all_users,
        "valid_roles": user_store.VALID_ROLES,
        "saved_email": email,
    })

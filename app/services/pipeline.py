"""
Funções compartilhadas entre os pipelines de upload e revogação.

Gerencia status de progresso, cancelamento e erros de jobs em background.
"""

from app.services import storage


class JobCancelled(Exception):
    pass


def raise_if_cancelled(job_id: str) -> None:
    status = storage.load_status(job_id) or {}
    if status.get("status") == "cancelled":
        raise JobCancelled()


def set_step(
    job_id: str,
    step: int,
    steps: list[tuple[int, str]],
    extra: dict | None = None,
) -> None:
    total = len(steps)
    label = steps[step - 1][1]
    pct = int((step - 1) / total * 100)
    data = {
        "id": job_id,
        "status": "processing",
        "current_step": step,
        "total_steps": total,
        "current_step_label": label,
        "progress_pct": pct,
    }
    if extra:
        data.update(extra)
    storage.save_status(job_id, data)


def set_done(
    job_id: str,
    result: dict,
    total_steps: int,
) -> None:
    storage.save_status(job_id, {
        "id": job_id,
        "status": "done",
        "current_step": total_steps,
        "total_steps": total_steps,
        "current_step_label": "Concluído",
        "progress_pct": 100,
        "result": result,
    })


def set_error(
    job_id: str,
    message: str,
    total_steps: int,
) -> None:
    storage.save_status(job_id, {
        "id": job_id,
        "status": "error",
        "error": message,
        "current_step": 0,
        "total_steps": total_steps,
        "current_step_label": "Erro",
        "progress_pct": 0,
    })

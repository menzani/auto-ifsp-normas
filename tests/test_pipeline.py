"""Testes para app/services/pipeline.py — gerenciamento de estado de jobs."""
import pytest

from app.services import storage
from app.services.pipeline import (
    JobCancelled,
    raise_if_cancelled,
    set_step,
    set_done,
    set_error,
)

SAMPLE_STEPS = [
    (1, "Etapa 1"),
    (2, "Etapa 2"),
    (3, "Concluído"),
]


class TestSetStep:
    def test_creates_status(self):
        set_step("job_s1", 1, SAMPLE_STEPS)
        status = storage.load_status("job_s1")
        assert status["status"] == "processing"
        assert status["current_step"] == 1
        assert status["current_step_label"] == "Etapa 1"
        assert status["progress_pct"] == 0

    def test_second_step_progress(self):
        set_step("job_s2", 2, SAMPLE_STEPS)
        status = storage.load_status("job_s2")
        assert status["current_step"] == 2
        assert status["progress_pct"] == 33  # int(1/3 * 100)

    def test_extra_fields_merged(self):
        set_step("job_s3", 1, SAMPLE_STEPS, extra={"owner": "user@test.com"})
        status = storage.load_status("job_s3")
        assert status["owner"] == "user@test.com"


class TestSetDone:
    def test_marks_as_done(self):
        storage.save_status("job_d1", {"id": "job_d1", "status": "processing"})
        set_done("job_d1", {"book_url": "/livro/1"}, 3)
        status = storage.load_status("job_d1")
        assert status["status"] == "done"
        assert status["progress_pct"] == 100
        assert status["result"]["book_url"] == "/livro/1"

    def test_does_not_overwrite_cancelled(self):
        storage.save_status("job_d2", {"id": "job_d2", "status": "cancelled"})
        set_done("job_d2", {"book_url": "/x"}, 3)
        status = storage.load_status("job_d2")
        assert status["status"] == "cancelled"


class TestSetError:
    def test_marks_as_error(self):
        storage.save_status("job_e1", {"id": "job_e1", "status": "processing"})
        set_error("job_e1", "Algo deu errado", 3)
        status = storage.load_status("job_e1")
        assert status["status"] == "error"
        assert status["error"] == "Algo deu errado"

    def test_does_not_overwrite_cancelled(self):
        storage.save_status("job_e2", {"id": "job_e2", "status": "cancelled"})
        set_error("job_e2", "Erro", 3)
        status = storage.load_status("job_e2")
        assert status["status"] == "cancelled"


class TestRaiseIfCancelled:
    def test_raises_when_cancelled(self):
        storage.save_status("job_c1", {"id": "job_c1", "status": "cancelled"})
        with pytest.raises(JobCancelled):
            raise_if_cancelled("job_c1")

    def test_no_raise_when_processing(self):
        storage.save_status("job_c2", {"id": "job_c2", "status": "processing"})
        raise_if_cancelled("job_c2")  # não deve lançar

    def test_no_raise_when_no_status(self):
        raise_if_cancelled("job_inexistente")  # não deve lançar

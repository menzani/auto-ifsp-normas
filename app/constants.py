"""
Constantes compartilhadas entre módulos da aplicação.
"""

# Job IDs gerados por secrets.token_urlsafe(16) — 22 caracteres base64url.
# Intervalo {10,60} tolera variações futuras sem quebrar validação existente.
JOB_ID_PATTERN = r"^[a-zA-Z0-9_-]{10,60}$"

# Jobs de revogação usam prefixo "rev_" para evitar colisão com jobs de upload.
REVOKE_JOB_ID_PATTERN = r"^rev_[a-zA-Z0-9_-]{10,60}$"

# IDs de entradas no registro de revogados (token sem prefixo).
REVOCATION_ID_PATTERN = r"^[a-zA-Z0-9_-]{10,60}$"

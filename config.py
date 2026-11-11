"""Central configuration for SpendPilot AI."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    SECRET_KEY = os.environ.get("SPENDPILOT_SECRET", "dev-only-change-me-" + "x" * 16)
    DATABASE = os.environ.get("SPENDPILOT_DB", os.path.join(BASE_DIR, "spendpilot.db"))
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf", "txt"}
    CSRF_ENABLED = True
    DEMO_MODE = os.environ.get("SPENDPILOT_DEMO", "1") == "1"  # shows demo logins on the login page
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # PayPal Sandbox credentials are read from the environment only.
    PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
    PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    PAYPAL_BASE_URL = "https://api-m.sandbox.paypal.com"

    # Optional Tesseract path on Windows, e.g. C:\Program Files\Tesseract-OCR\tesseract.exe
    TESSERACT_CMD = os.environ.get("TESSERACT_CMD", "")

    # Roles
    ROLES = ("super_admin", "finance_admin", "manager", "employee", "auditor")

    # Expense workflow statuses
    STATUSES = (
        "DRAFT", "SUBMITTED", "ANALYSING", "FLAGGED",
        "PENDING_MANAGER_APPROVAL", "MANAGER_APPROVED",
        "PENDING_FINANCE_AUTHORIZATION", "FINANCE_AUTHORIZED",
        "PAYMENT_PROCESSING", "PAID", "REJECTED", "PAYMENT_FAILED",
    )

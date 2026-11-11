"""Authentication, session handling and role-based access decorators."""
import re
import time
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from pydantic import BaseModel, ValidationError, field_validator
from werkzeug.security import check_password_hash, generate_password_hash

from database.connection import get_db, transaction
from services.audit_service import log_event

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_FAILED = {}  # email -> (count, first_failure_ts); simple in-memory lockout
MAX_ATTEMPTS, LOCK_SECONDS = 5, 300


class LoginForm(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        v = v.strip().lower()
        if not EMAIL_RE.match(v) or len(v) > 254:
            raise ValueError("Enter a valid email address")
        return v


class RegisterForm(BaseModel):
    name: str
    email: str
    password: str
    department_id: int

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = v.strip()
        if not 2 <= len(v) <= 80:
            raise ValueError("Name must be 2-80 characters")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        v = v.strip().lower()
        if not EMAIL_RE.match(v) or len(v) > 254:
            raise ValueError("Enter a valid email address")
        return v

    @field_validator("password")
    @classmethod
    def _pw(cls, v):
        if len(v) < 8 or not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("Password needs 8+ characters with letters and digits")
        return v


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    return err["msg"].replace("Value error, ", "")


@bp.before_app_request
def load_user():
    g.user = None
    uid = session.get("user_id")
    if uid is not None:
        row = get_db().execute(
            "SELECT id, org_id, department_id, name, email, role, paypal_email FROM users"
            " WHERE id = ? AND is_active = 1", (uid,)).fetchone()
        if row is None:
            session.clear()
        else:
            g.user = row


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/") or request.is_json:
                return jsonify(error="Authentication required"), 401
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    """Backend role enforcement. Always applied server-side, never only in the UI."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in roles:
                if request.path.startswith("/api/") or request.is_json:
                    return jsonify(error="Forbidden for your role"), 403
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("analytics.dashboard"))
    if request.method == "POST":
        try:
            form = LoginForm(email=request.form.get("email", ""), password=request.form.get("password", ""))
        except ValidationError as exc:
            flash(_first_error(exc), "danger")
            return render_template("login.html", orgs=_departments()), 400
        count, first = _FAILED.get(form.email, (0, 0))
        if count >= MAX_ATTEMPTS and time.time() - first < LOCK_SECONDS:
            flash("Too many failed attempts. Try again in a few minutes.", "danger")
            return render_template("login.html", orgs=_departments()), 429
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ? AND is_active = 1", (form.email,)).fetchone()
        if user and check_password_hash(user["password_hash"], form.password):
            _FAILED.pop(form.email, None)
            session.clear()  # prevent session fixation
            session["user_id"] = user["id"]
            with transaction(db):
                log_event(db, user["org_id"], "LOGIN", actor_id=user["id"])
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("analytics.dashboard"))
        if count == 0 or time.time() - first >= LOCK_SECONDS:
            _FAILED[form.email] = (1, time.time())
        else:
            _FAILED[form.email] = (count + 1, first)
        flash("Invalid email or password.", "danger")
        return render_template("login.html", orgs=_departments()), 401
    return render_template("login.html", orgs=_departments())


def _departments():
    return get_db().execute(
        "SELECT d.id, d.name, o.name AS org FROM departments d JOIN organizations o ON o.id = d.org_id"
        " ORDER BY o.name, d.name").fetchall()


@bp.route("/register", methods=["POST"])
def register():
    """Self-registration creates employee accounts only; elevated roles are assigned by a Super Admin."""
    try:
        form = RegisterForm(
            name=request.form.get("name", ""), email=request.form.get("email", ""),
            password=request.form.get("password", ""),
            department_id=request.form.get("department_id", "0"))
    except ValidationError as exc:
        flash(_first_error(exc), "danger")
        return redirect(url_for("auth.login") + "#register")
    db = get_db()
    dept = db.execute("SELECT id, org_id FROM departments WHERE id = ?", (form.department_id,)).fetchone()
    if dept is None:
        flash("Select a valid department.", "danger")
        return redirect(url_for("auth.login") + "#register")
    if db.execute("SELECT 1 FROM users WHERE email = ?", (form.email,)).fetchone():
        flash("That email is already registered.", "danger")
        return redirect(url_for("auth.login") + "#register")
    with transaction(db):
        uid = db.execute(
            "INSERT INTO users (org_id, department_id, name, email, password_hash, role, paypal_email)"
            " VALUES (?,?,?,?,?,'employee',?)",
            (dept["org_id"], dept["id"], form.name, form.email,
             generate_password_hash(form.password), form.email)).lastrowid
        log_event(db, dept["org_id"], "USER_REGISTERED", actor_id=uid, details={"email": form.email})
    flash("Account created. Please sign in.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("auth.login"))

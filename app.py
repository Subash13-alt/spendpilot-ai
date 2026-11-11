"""SpendPilot AI - application entry point.

Run with:  python app.pypytho
"""
import os
import secrets

from flask import Flask, abort, jsonify, render_template, request, session

from config import Config
from database.connection import close_db, connect
from database.schema import init_schema
from database.seed import seed_base


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    # --- database: create tables and demo data on startup ---
    conn = connect(app.config["DATABASE"])
    seeded_now = False
    try:
        init_schema(conn)
        if app.config.get("SEED_DEMO", True):
            seeded_now = seed_base(conn)
    finally:
        conn.close()
    app.teardown_appcontext(close_db)

    # --- CSRF protection for every state-changing request ---
    @app.before_request
    def csrf_protect():
        if not app.config.get("CSRF_ENABLED", True):
            return
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
            expected = session.get("_csrf", "")
            if not expected or not secrets.compare_digest(expected, sent):
                abort(400, description="Invalid or missing CSRF token")

    @app.context_processor
    def inject_globals():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        return {"csrf_token": session["_csrf"], "demo_mode": app.config.get("DEMO_MODE", False)}

    # --- blueprints ---
    from routes import auth, analytics
    app.register_blueprint(auth.bp)
    app.register_blueprint(analytics.bp)
    for name in ("expenses", "approvals", "agents", "payments", "admin"):
        try:
            module = __import__(f"routes.{name}", fromlist=["bp"])
        except ModuleNotFoundError as exc:  # modules are added phase by phase
            if exc.name != f"routes.{name}":
                raise
            continue
        app.register_blueprint(module.bp)

    # --- agents / NLP warm-up (added in later phases) ---
    try:
        from agents.orchestrator import init_orchestrator
        init_orchestrator(app)
    except ModuleNotFoundError as exc:
        if exc.name != "agents.orchestrator":
            raise

    # --- demonstration scenarios run through the real pipeline, once, on first start ---
    if seeded_now and app.config.get("SEED_DEMO_WORKFLOW", True):
        from database.seed import seed_demo_workflow
        conn = connect(app.config["DATABASE"])
        try:
            results = seed_demo_workflow(conn, app.extensions["orchestrator"], app.config["UPLOAD_FOLDER"])
            app.logger.info("Demo scenarios loaded: %s", results)
        finally:
            conn.close()

    @app.errorhandler(403)
    def forbidden(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="Forbidden"), 403
        return render_template("error.html", code=403, message="You do not have access to this page."), 403

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found"), 404
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(400)
    def bad_request(e):
        if request.path.startswith("/api/") or request.is_json:
            return jsonify(error=getattr(e, "description", "Bad request")), 400
        return render_template("error.html", code=400, message=getattr(e, "description", "Bad request")), 400

    @app.errorhandler(413)
    def too_large(_e):
        return render_template("error.html", code=413, message="Upload exceeds the 5 MB limit."), 413

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)

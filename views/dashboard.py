from flask import Blueprint

# Dashboard data is served entirely through /api/dashboard (views/api.py).
# This blueprint is kept so the import in app.py remains valid.
dashboard_bp = Blueprint('dashboard', __name__)

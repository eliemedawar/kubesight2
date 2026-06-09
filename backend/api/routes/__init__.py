from flask import Flask

from .dashboard import dashboard_bp
from .alerts import alerts_bp
from .alert_policies import alert_policies_bp
from .audit_logs import audit_bp
from .auth import auth_bp
from .clusters import clusters_bp
from .logs import logs_bp
from .roles import roles_bp
from .settings import settings_bp
from .upgrades import upgrades_bp
from .access_rules import access_rules_bp
from .users import users_bp
from .inventory import inventory_bp
from .helm import helm_bp
from .alert_routing import alert_routing_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(auth_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(access_rules_bp)
    app.register_blueprint(roles_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(clusters_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(alerts_bp)
    app.register_blueprint(alert_policies_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(upgrades_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(helm_bp)
    app.register_blueprint(alert_routing_bp)

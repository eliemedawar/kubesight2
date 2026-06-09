import os

from api import create_app

app = create_app()

if __name__ == "__main__":
    from api.services.alert_policy_scheduler import start_alert_policy_scheduler

    start_alert_policy_scheduler(app)
    debug = os.getenv("FLASK_DEBUG", "true").strip().lower() in {"1", "true", "yes", "on"}
    app.run(host="0.0.0.0", port=5000, debug=debug, use_reloader=debug)

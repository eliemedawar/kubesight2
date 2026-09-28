from api import create_app

app = create_app()

if __name__ == "__main__":
    from api.services.alert_policy_scheduler import start_alert_policy_scheduler
    from api.services.ci.ticker import start_ci_engine

    start_alert_policy_scheduler(app)
    start_ci_engine(app)
    from api.runtime_config import env_flag, is_production_env

    # The dev server defaults to debug for a laptop, never for production: the
    # Werkzeug debugger is remote code execution for whoever can reach the port.
    debug = env_flag("FLASK_DEBUG", default=True) and not is_production_env()
    app.run(host="0.0.0.0", port=5000, debug=debug, use_reloader=debug)

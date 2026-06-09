from typing import Any, Optional

from flask import jsonify


def success_response(data: Any, status_code: int = 200):
    return jsonify({"success": True, "data": data, "error": None}), status_code


def error_response(message: str, status_code: int = 400, data: Optional[Any] = None):
    return jsonify({"success": False, "data": data, "error": message}), status_code

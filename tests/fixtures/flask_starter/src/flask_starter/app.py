"""Main Flask app entry point with a planted bare except clause."""
from flask import Flask, jsonify, request

from flask_starter.config import API_KEY, DEBUG
from flask_starter.models import User

app = Flask(__name__)
app.config["DEBUG"] = DEBUG


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/users", methods=["POST"])
def create_user():
    data = request.get_json()
    # PLANTED ISSUE: bare except swallows every error, including KeyboardInterrupt.
    try:
        user = User(username=data["username"], email=data["email"])
        return jsonify({"username": user.username, "email": user.email}), 201
    except:
        return jsonify({"error": "something went wrong"}), 500


@app.route("/auth")
def auth():
    key = request.headers.get("X-API-Key")
    # PLANTED ISSUE: plaintext key comparison — should use hmac.compare_digest.
    if key == API_KEY:
        return jsonify({"authenticated": True})
    return jsonify({"authenticated": False}), 401


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

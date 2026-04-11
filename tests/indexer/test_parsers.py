"""Tests for the deterministic code parsers."""

import pytest
from src.indexer.parsers import (
    parse_file, parse_python, parse_javascript, parse_config, parse_generic,
    detect_language, compute_hash, _extract_todos,
)


# ── Language Detection ─────────────────────────────────────────────

class TestLanguageDetection:
    def test_python(self):
        assert detect_language("src/main.py") == "python"

    def test_javascript(self):
        assert detect_language("app.js") == "javascript"
        assert detect_language("components/App.jsx") == "javascript"

    def test_typescript(self):
        assert detect_language("src/api.ts") == "typescript"
        assert detect_language("components/App.tsx") == "typescript"

    def test_json(self):
        assert detect_language("package.json") == "json"

    def test_yaml(self):
        assert detect_language("config.yml") == "yaml"
        assert detect_language("docker-compose.yaml") == "yaml"

    def test_dockerfile(self):
        assert detect_language("Dockerfile") == "docker"
        assert detect_language("src/Dockerfile") == "docker"

    def test_makefile(self):
        assert detect_language("Makefile") == "shell"

    def test_unknown(self):
        assert detect_language("random.xyz") == "other"

    def test_vue(self):
        assert detect_language("Component.vue") == "vue"

    def test_sql(self):
        assert detect_language("schema.sql") == "sql"


# ── Hash ───────────────────────────────────────────────────────────

class TestHash:
    def test_deterministic(self):
        assert compute_hash("hello") == compute_hash("hello")

    def test_different_content(self):
        assert compute_hash("hello") != compute_hash("world")

    def test_returns_64_char_hex(self):
        h = compute_hash("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ── TODO Extraction ────────────────────────────────────────────────

class TestTodoExtraction:
    def test_finds_todo(self):
        content = "# TODO: fix this\ncode here\n# FIXME: broken"
        todos = _extract_todos(content)
        assert len(todos) == 2
        assert todos[0]["line"] == 1
        assert "TODO" in todos[0]["text"]
        assert todos[1]["line"] == 3
        assert "FIXME" in todos[1]["text"]

    def test_no_todos(self):
        content = "clean code\nno issues here"
        assert _extract_todos(content) == []

    def test_hack_and_xxx(self):
        content = "# HACK: workaround\n# XXX: needs review"
        todos = _extract_todos(content)
        assert len(todos) == 2


# ── Python Parser ──────────────────────────────────────────────────

class TestPythonParser:
    def test_basic_function(self):
        code = '''
def hello(name: str) -> str:
    """Say hello."""
    return f"Hello {name}"
'''
        result = parse_python(code, "test.py")
        assert result.language == "python"
        assert result.line_count == 5
        assert len(result.structure["functions"]) == 1
        fn = result.structure["functions"][0]
        assert fn["name"] == "hello"
        assert "name" in fn["args"]

    def test_async_function(self):
        code = '''
async def fetch_data(url: str, timeout: int = 30):
    pass
'''
        result = parse_python(code, "test.py")
        fn = result.structure["functions"][0]
        assert fn["name"] == "fetch_data"
        assert fn["is_async"] is True
        assert "url" in fn["args"]

    def test_class_with_methods(self):
        code = '''
class UserService(BaseService):
    name = "user"

    def __init__(self, db):
        self.db = db

    async def create_user(self, email, password):
        pass

    def get_user(self, user_id):
        pass
'''
        result = parse_python(code, "services/user.py")
        assert len(result.structure["classes"]) == 1
        cls = result.structure["classes"][0]
        assert cls["name"] == "UserService"
        assert "BaseService" in cls["bases"]
        assert "__init__" in cls["methods"]
        assert "create_user" in cls["methods"]
        assert "get_user" in cls["methods"]

    def test_imports(self):
        code = '''
import os
import json
from flask import Flask, request
from src.models import User
'''
        result = parse_python(code, "app.py")
        imports = result.structure["imports"]
        assert "os" in imports
        assert "json" in imports
        assert "flask" in imports
        assert "src.models" in imports

    def test_route_decorators(self):
        code = '''
from flask import Flask
app = Flask(__name__)

@app.route("/api/health", methods=["GET"])
def health():
    return {"status": "ok"}

@app.post("/api/users")
async def create_user():
    pass
'''
        result = parse_python(code, "routes.py")
        routes = result.structure["routes"]
        assert len(routes) >= 1
        assert any(r["path"] == "/api/health" for r in routes)

    def test_constants(self):
        code = '''
MAX_RETRIES = 3
DEBUG = True
API_VERSION = "v2"
'''
        result = parse_python(code, "config.py")
        constants = result.structure["constants"]
        names = [c["name"] for c in constants]
        assert "MAX_RETRIES" in names
        assert "DEBUG" in names
        assert "API_VERSION" in names

    def test_syntax_error_fallback(self):
        code = "def broken(:\n    pass"
        result = parse_python(code, "broken.py")
        assert result.structure.get("parse_error") is True
        assert result.language == "python"
        assert result.line_count == 2

    def test_class_fields(self):
        code = '''
class User(Base):
    __tablename__ = "users"
    id: int
    email: str
    name: str
    created_at: datetime
'''
        result = parse_python(code, "models/user.py")
        cls = result.structure["classes"][0]
        assert "id" in cls["fields"]
        assert "email" in cls["fields"]

    def test_empty_file(self):
        result = parse_python("", "empty.py")
        assert result.line_count == 1  # empty string splits to ['']
        assert result.structure["functions"] == []
        assert result.structure["classes"] == []

    def test_todos_in_python(self):
        code = '''
def process():
    # TODO: add error handling
    # FIXME: this is slow
    pass
'''
        result = parse_python(code, "process.py")
        assert len(result.structure["todos"]) == 2


# ── JavaScript Parser ─────────────────────────────────────────────

class TestJavaScriptParser:
    def test_imports(self):
        code = '''
import React from 'react';
import { useState, useEffect } from 'react';
const express = require('express');
'''
        result = parse_javascript(code, "app.js")
        imports = result.structure["imports"]
        assert "react" in imports
        assert "express" in imports

    def test_functions(self):
        code = '''
function handleClick(event) {
  console.log(event);
}

const fetchData = async (url) => {
  return fetch(url);
}
'''
        result = parse_javascript(code, "utils.js")
        functions = result.structure["functions"]
        names = [f["name"] for f in functions]
        assert "handleClick" in names
        assert "fetchData" in names

    def test_classes(self):
        code = '''
class ApiClient extends BaseClient {
  constructor(url) {
    super(url);
  }
}

export default class UserService {
  async getUser(id) {}
}
'''
        result = parse_javascript(code, "api.js")
        classes = result.structure["classes"]
        assert len(classes) == 2
        assert classes[0]["name"] == "ApiClient"
        assert classes[0]["extends"] == "BaseClient"

    def test_routes(self):
        code = '''
app.get('/api/users', getUsers);
app.post('/api/users', createUser);
router.delete('/api/users/:id', deleteUser);
'''
        result = parse_javascript(code, "routes.js")
        routes = result.structure["routes"]
        assert len(routes) == 3
        assert routes[0]["method"] == "GET"
        assert routes[0]["path"] == "/api/users"

    def test_react_components(self):
        code = '''
function Dashboard(props) {
  return <div>Dashboard</div>;
}

const UserProfile = (props) => {
  return <div>Profile</div>;
}
'''
        result = parse_javascript(code, "components.jsx")
        components = result.structure["components"]
        assert "Dashboard" in components

    def test_exports(self):
        code = '''
export function helper() {}
export const API_URL = "http://...";
export default class App {}
'''
        result = parse_javascript(code, "module.js")
        exports = result.structure["exports"]
        assert "helper" in exports

    def test_typescript(self):
        result = parse_javascript("const x: number = 5;", "app.ts")
        assert result.language == "typescript"

    def test_fetch_calls(self):
        code = '''
fetch('/api/data').then(r => r.json());
fetch(`/api/users/${id}`);
'''
        result = parse_javascript(code, "client.js")
        api_calls = result.structure["api_calls"]
        assert "/api/data" in api_calls


# ── Config Parser ──────────────────────────────────────────────────

class TestConfigParser:
    def test_package_json(self):
        code = '''{
  "name": "my-app",
  "scripts": {"start": "node index.js", "test": "jest"},
  "dependencies": {"express": "^4.18.0", "cors": "^2.8.5"},
  "devDependencies": {"jest": "^29.0.0"}
}'''
        result = parse_config(code, "package.json")
        struct = result.structure
        assert struct["name"] == "my-app"
        assert "start" in struct["scripts"]
        assert "express" in struct["dependencies"]
        assert "jest" in struct["dev_dependencies"]

    def test_yaml(self):
        code = '''
services:
  app:
    build: .
  db:
    image: postgres
  redis:
    image: redis
'''
        result = parse_config(code, "docker-compose.yml")
        assert result.language == "yaml"
        assert "app" in result.structure.get("services", [])

    def test_invalid_json(self):
        result = parse_config("{invalid json", "bad.json")
        assert result.structure.get("parse_error") is True

    def test_toml(self):
        code = '''
[project]
name = "my-project"
dependencies = ["flask", "sqlalchemy"]
'''
        result = parse_config(code, "pyproject.toml")
        assert result.language == "toml"
        # tomllib only available in Python 3.11+; may fall back to parse_error
        if not result.structure.get("parse_error"):
            assert result.structure.get("name") == "my-project"
            assert "flask" in result.structure.get("dependencies", [])


# ── Generic Parser ─────────────────────────────────────────────────

class TestGenericParser:
    def test_basic(self):
        content = "line 1\nline 2\nline 3"
        result = parse_generic(content, "readme.txt")
        assert result.line_count == 3
        assert result.size_bytes == len(content.encode("utf-8"))
        assert result.content_hash != ""

    def test_with_todos(self):
        content = "# Some doc\n<!-- TODO: write docs -->\nMore text"
        result = parse_generic(content, "docs.md")
        assert len(result.structure["todos"]) == 1


# ── Dispatcher ─────────────────────────────────────────────────────

class TestParseFile:
    def test_dispatches_python(self):
        result = parse_file("def foo(): pass", "main.py")
        assert result.language == "python"
        assert len(result.structure["functions"]) == 1

    def test_dispatches_javascript(self):
        result = parse_file("function foo() {}", "app.js")
        assert result.language == "javascript"

    def test_dispatches_json(self):
        result = parse_file('{"key": "value"}', "config.json")
        assert result.language == "json"

    def test_dispatches_generic_for_unknown(self):
        result = parse_file("some content", "file.xyz")
        assert result.language == "other"

    def test_vue_uses_js_parser(self):
        code = '''
<script>
import { ref } from 'vue';
export default { name: 'MyComponent' }
</script>
'''
        result = parse_file(code, "Component.vue")
        assert result.language == "vue"
        assert "vue" in result.structure.get("imports", [])

    def test_content_hash_populated(self):
        result = parse_file("test content", "test.py")
        assert len(result.content_hash) == 64


# ── Code Map Integration ──────────────────────────────────────────

class TestCodeMapIntegration:
    """Test that parsed data produces a useful code map."""

    def test_full_project_simulation(self):
        from src.indexer.code_map import generate_code_map

        # Simulate parsing a small project
        records = [
            {
                "file_path": "backend/app/main.py",
                "language": "python",
                "line_count": 30,
                "structure": {
                    "imports": ["flask", "sqlalchemy"],
                    "classes": [],
                    "functions": [{"name": "create_app", "args": [], "decorators": [], "is_async": False}],
                    "routes": [{"method": "GET", "path": "/api/health", "handler": "health"}],
                    "constants": [],
                    "todos": [],
                },
            },
            {
                "file_path": "backend/app/models/user.py",
                "language": "python",
                "line_count": 25,
                "structure": {
                    "imports": ["sqlalchemy"],
                    "classes": [{"name": "User", "bases": ["Base"], "methods": ["to_dict"], "fields": ["id", "email", "name"], "line": 5}],
                    "functions": [],
                    "routes": [],
                    "constants": [],
                    "todos": [],
                },
            },
            {
                "file_path": "frontend/app.js",
                "language": "javascript",
                "line_count": 50,
                "structure": {
                    "imports": ["react"],
                    "exports": ["App"],
                    "functions": [{"name": "App", "line": 1}],
                    "classes": [],
                    "routes": [],
                    "components": ["App"],
                    "api_calls": ["/api/health"],
                    "todos": [{"line": 10, "text": "TODO: add error handling"}],
                },
            },
            {
                "file_path": "package.json",
                "language": "json",
                "line_count": 15,
                "structure": {
                    "top_level_keys": ["name", "dependencies"],
                    "name": "test-project",
                    "dependencies": ["react", "express"],
                    "dev_dependencies": ["jest"],
                },
            },
        ]

        code_map = generate_code_map(records, project_name="test/project")

        # Verify key sections exist
        assert "test/project" in code_map
        assert "python" in code_map.lower()
        assert "javascript" in code_map.lower()
        assert "/api/health" in code_map
        assert "User" in code_map
        assert "TODO" in code_map
        # Repository Facts section replaces the old judgmental "Detected Gaps"
        assert "## Repository Facts" in code_map
        assert "test_files: 0" in code_map

        # Verify it's compact
        assert len(code_map) < 5000  # should be well under 5KB for 4 files

    def test_empty_records(self):
        from src.indexer.code_map import generate_code_map

        result = generate_code_map([])
        assert "Empty Repository" in result

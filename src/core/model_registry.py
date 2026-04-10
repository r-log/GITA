"""
Model Registry — resolves which LLM model each agent/workload should use.

Reads from plans/model_assignments.yml (produced by Phase 0 bake-off).
Falls back to per-agent env vars (AI_MODEL_*) if the YAML is missing.
Falls back to AI_DEFAULT_MODEL if neither exists.

Usage:
    from src.core.model_registry import model_registry
    model = model_registry.model_for("issue_analyst")       # -> "moonshotai/kimi-k2.5"
    fallback = model_registry.fallback_for("issue_analyst")  # -> "deepseek/deepseek-chat"
"""

from pathlib import Path

import structlog
import yaml

from src.core.config import settings

log = structlog.get_logger()

# Look for model_assignments.yml next to this file in src/core/
_YAML_PATHS = [
    Path(__file__).resolve().parent / "model_assignments.yml",
    Path("src/core/model_assignments.yml"),
]

# Mapping from agent name to the settings attribute for env-var fallback
_ENV_VAR_MAP = {
    "issue_analyst": "ai_model_issue_analyst",
    "pr_reviewer": "ai_model_pr_reviewer",
    "risk_detective": "ai_model_risk_detective",
    "progress_tracker": "ai_model_progress_tracker",
    "onboarding": "ai_model_onboarding_pass3",
    "test_author": "ai_model_pr_reviewer",  # closest available default
}


class ModelRegistry:
    """Resolves (agent_name) -> model_id from YAML, env vars, or defaults."""

    def __init__(self):
        self._agents: dict[str, dict] = {}
        self._preprocessing: dict[str, dict] = {}
        self._embeddings: dict = {}
        self._loaded = False
        self._load()

    def _load(self):
        """Load model_assignments.yml if it exists."""
        for path in _YAML_PATHS:
            if path.exists():
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8"))
                    if data:
                        self._agents = data.get("agents", {})
                        self._preprocessing = data.get("preprocessing", {})
                        self._embeddings = data.get("embeddings", {})
                        self._loaded = True
                        log.info("model_registry_loaded", path=str(path), agents=len(self._agents))
                        return
                except Exception as e:
                    log.warning("model_registry_load_failed", path=str(path), error=str(e))

        log.info("model_registry_no_yaml", msg="Using env var defaults")

    def model_for(self, agent_name: str) -> str:
        """
        Get the primary model for an agent.

        Resolution order:
        1. model_assignments.yml -> agents.<name>.primary
        2. Settings env var (AI_MODEL_<NAME>)
        3. settings.ai_default_model
        """
        # 1. YAML
        if self._loaded and agent_name in self._agents:
            primary = self._agents[agent_name].get("primary")
            if primary:
                return primary

        # 2. Env var
        env_attr = _ENV_VAR_MAP.get(agent_name)
        if env_attr and hasattr(settings, env_attr):
            return getattr(settings, env_attr)

        # 3. Default
        return settings.ai_default_model

    def fallback_for(self, agent_name: str) -> str | None:
        """Get the fallback model for an agent, or None if no fallback configured."""
        if self._loaded and agent_name in self._agents:
            return self._agents[agent_name].get("fallback")
        return None

    def preprocessing_model(self, task: str) -> str:
        """Get model for a preprocessing task (classification, summarization)."""
        if self._loaded and task in self._preprocessing:
            return self._preprocessing[task].get("primary", settings.ai_default_model)
        return settings.ai_default_model

    def embedding_config(self) -> dict:
        """Get embedding configuration."""
        if self._loaded and self._embeddings:
            return self._embeddings
        return {"primary": "local/nomic-embed-text", "dimensions": 768, "backend": "ollama"}

    @property
    def is_loaded(self) -> bool:
        """Whether model_assignments.yml was found and loaded."""
        return self._loaded

    def summary(self) -> dict[str, str]:
        """Return a dict of agent_name -> resolved model for logging."""
        agents = list(_ENV_VAR_MAP.keys())
        return {name: self.model_for(name) for name in agents}


# Singleton — import this everywhere
model_registry = ModelRegistry()

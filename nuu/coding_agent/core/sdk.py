"""
SDK services for creating agent sessions. Assembles SettingsManager,
ModelRegistry, ResourceLoader, and AuthStorage into an AgentSessionServices
container. Also provides convenience functions for building sessions and
tools.

Owns: AgentSessionServices, create_agent_session_services(),
  create_agent_session_from_services(), AuthStorage, PromptTemplate,
  create_coding_tools(), create_read_only_tools().
Delegates to: settings_manager, model_registry, resource_loader, session.

Data flow: cwd + agent_dir -> create_agent_session_services() ->
  AgentSessionServices -> create_agent_session_from_services() -> AgentSession

Depends on: nuu.coding_agent.core.settings_manager,
  nuu.coding_agent.core.model_registry, nuu.coding_agent.core.resource_loader,
  nuu.coding_agent.session, nuu.coding_agent.session_manager,
  nuu.coding_agent.tools.*, nuu.coding_agent.config
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nuu.coding_agent.core.settings_manager import SettingsManager
from nuu.coding_agent.core.model_registry import ModelRegistry
from nuu.coding_agent.core.resource_loader import ResourceLoader
from nuu.coding_agent.config import get_agent_dir


class PromptTemplate:
    def __init__(self, template: str):
        self.template = template

    def __call__(self, **kwargs: str) -> str:
        return self.template.format(**kwargs)


def create_coding_tools(cwd: str | None = None) -> list:
    from nuu.coding_agent.tools.read import ReadTool
    from nuu.coding_agent.tools.write import WriteTool
    from nuu.coding_agent.tools.edit import EditTool
    from nuu.coding_agent.tools.bash import BashTool
    from nuu.coding_agent.tools.ls import LsTool
    from nuu.coding_agent.tools.find import FindTool
    from nuu.coding_agent.tools.grep import GrepTool

    resolved_cwd = cwd or os.getcwd()
    return [
        ReadTool(cwd=resolved_cwd),
        WriteTool(cwd=resolved_cwd),
        EditTool(cwd=resolved_cwd),
        BashTool(cwd=resolved_cwd),
        LsTool(cwd=resolved_cwd),
        FindTool(cwd=resolved_cwd),
        GrepTool(cwd=resolved_cwd),
    ]


def create_read_only_tools(cwd: str | None = None) -> list:
    from nuu.coding_agent.tools.read import ReadTool
    from nuu.coding_agent.tools.ls import LsTool
    from nuu.coding_agent.tools.find import FindTool
    from nuu.coding_agent.tools.grep import GrepTool

    resolved_cwd = cwd or os.getcwd()
    return [
        ReadTool(cwd=resolved_cwd),
        LsTool(cwd=resolved_cwd),
        FindTool(cwd=resolved_cwd),
        GrepTool(cwd=resolved_cwd),
    ]


class AuthStorage:
    def __init__(self, path: str | None = None):
        self.path = path
        self._credentials: dict[str, str] = {}

    def get_api_key(self, provider: str) -> str | None:
        from nuu.ai.env_api_keys import get_env_api_key

        key = get_env_api_key(provider)
        if key:
            return key
        return self._credentials.get(provider)

    def set_api_key(self, provider: str, key: str):
        self._credentials[provider] = key

    @staticmethod
    def create(path: str | None = None) -> AuthStorage:
        return AuthStorage(path)


@dataclass
class AgentSessionServices:
    settings_manager: SettingsManager
    model_registry: ModelRegistry
    resource_loader: ResourceLoader
    auth_storage: AuthStorage


def create_agent_session_services(
    cwd: str | None = None,
    agent_dir: str | None = None,
) -> AgentSessionServices:
    cwd = cwd or os.getcwd()
    agent_dir = agent_dir or str(get_agent_dir())

    auth_storage = AuthStorage.create(os.path.join(agent_dir, "auth.json"))
    model_registry = ModelRegistry(Path(agent_dir) / "models.json")
    settings_manager = SettingsManager(Path(agent_dir) / "settings.json")
    resource_loader = ResourceLoader(cwd=Path(cwd), agent_dir=Path(agent_dir))

    return AgentSessionServices(
        settings_manager=settings_manager,
        model_registry=model_registry,
        resource_loader=resource_loader,
        auth_storage=auth_storage,
    )


async def create_agent_session_from_services(
    services: AgentSessionServices,
    model: Any = None,
    system_prompt: str = "",
    cwd: str | None = None,
    session_manager: Any = None,
) -> Any:
    from nuu.coding_agent.session import AgentSession
    from nuu.coding_agent.session_manager import SessionManager

    cwd = cwd or os.getcwd()
    session_manager = session_manager or SessionManager.create(cwd)

    if model is None:
        from nuu.ai.models import get_model

        provider = services.settings_manager.get("default_provider")
        model_id = services.settings_manager.get("default_model")
        if provider and model_id:
            model = get_model(provider, model_id)

        if model is None:
            available = services.model_registry.get_models(provider) if provider else []
            if not available:
                raise RuntimeError(
                    "No model available. Configure a default provider and model."
                )
            model = available[0]

    session = AgentSession(
        cwd=cwd,
        model=model,
        system_prompt=system_prompt,
        session_manager=session_manager,
    )

    return session


async def create_agent_session(
    cwd: str | None = None,
    agent_dir: str | None = None,
    model: Any = None,
    system_prompt: str = "",
    session_manager: Any = None,
) -> Any:
    services = create_agent_session_services(cwd=cwd, agent_dir=agent_dir)
    await services.resource_loader.reload()
    return await create_agent_session_from_services(
        services=services,
        model=model,
        system_prompt=system_prompt,
        cwd=cwd,
        session_manager=session_manager,
    )


class AgentSessionRuntime:
    def __init__(
        self,
        session: Any,
        services: AgentSessionServices,
        cwd: str,
    ):
        self._session = session
        self._services = services
        self._cwd = cwd

    @property
    def session(self) -> Any:
        return self._session

    @property
    def services(self) -> AgentSessionServices:
        return self._services

    @property
    def cwd(self) -> str:
        return self._cwd

    async def dispose(self):
        pass


async def create_agent_session_runtime(
    cwd: str | None = None,
    agent_dir: str | None = None,
    model: Any = None,
    system_prompt: str = "",
) -> AgentSessionRuntime:
    cwd = cwd or os.getcwd()
    agent_dir = agent_dir or str(get_agent_dir())

    services = create_agent_session_services(cwd=cwd, agent_dir=agent_dir)
    await services.resource_loader.reload()
    session = await create_agent_session_from_services(
        services=services,
        model=model,
        system_prompt=system_prompt,
        cwd=cwd,
    )

    return AgentSessionRuntime(
        session=session,
        services=services,
        cwd=cwd,
    )

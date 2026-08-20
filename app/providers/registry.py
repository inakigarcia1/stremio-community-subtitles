"""
Provider Registry - Central registry for all subtitle providers.
"""
import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Dict, List, Optional

from .base import BaseSubtitleProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    _providers: Dict[str, BaseSubtitleProvider] = {}
    _initialized = False

    @classmethod
    def register(cls, provider_class: type) -> BaseSubtitleProvider:
        if not issubclass(provider_class, BaseSubtitleProvider):
            raise TypeError(f"{provider_class} must inherit from BaseSubtitleProvider")

        provider = provider_class()

        if provider.name in cls._providers:
            raise ValueError(f"Provider '{provider.name}' is already registered")

        cls._providers[provider.name] = provider
        return provider

    @classmethod
    def get(cls, name: str) -> Optional[BaseSubtitleProvider]:
        return cls._providers.get(name)

    @classmethod
    def get_all(cls, user=None, filter_by_language: bool = True) -> List[BaseSubtitleProvider]:
        providers = list(cls._providers.values())

        if user and filter_by_language and hasattr(user, 'preferred_languages') and user.preferred_languages:
            filtered = []
            for provider in providers:
                if provider.supported_languages is None:
                    filtered.append(provider)
                elif any(lang in provider.supported_languages for lang in user.preferred_languages):
                    filtered.append(provider)
            return filtered

        return providers

    @classmethod
    async def get_active_for_user(cls, user) -> List[BaseSubtitleProvider]:
        active_providers = []
        for provider in cls._providers.values():
            if await provider.is_authenticated(user):
                active_providers.append(provider)
        return active_providers

    @classmethod
    def get_by_auth_requirement(cls, requires_auth: bool) -> List[BaseSubtitleProvider]:
        return [
            provider for provider in cls._providers.values()
            if provider.requires_auth == requires_auth
        ]

    @classmethod
    def clear(cls):
        cls._providers.clear()
        cls._initialized = False

    @classmethod
    def _discover_provider_classes(cls):
        """Import provider modules under app/providers and register subclasses."""
        providers_root = Path(__file__).resolve().parent
        package_name = __package__

        for module_info in pkgutil.iter_modules([str(providers_root)]):
            if module_info.name in ("base", "registry"):
                continue

            module_path = providers_root / module_info.name
            if module_path.is_dir():
                for candidate in ("provider", "__init__"):
                    try:
                        module = importlib.import_module(f"{package_name}.{module_info.name}.{candidate}")
                    except ImportError:
                        continue
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if (
                            isinstance(attr, type)
                            and issubclass(attr, BaseSubtitleProvider)
                            and attr is not BaseSubtitleProvider
                        ):
                            try:
                                cls.register(attr)
                            except ValueError:
                                pass
                            break
                    else:
                        continue
                    break
            else:
                try:
                    module = importlib.import_module(f"{package_name}.{module_info.name}")
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if (
                            isinstance(attr, type)
                            and issubclass(attr, BaseSubtitleProvider)
                            and attr is not BaseSubtitleProvider
                        ):
                            try:
                                cls.register(attr)
                            except ValueError:
                                pass
                except ImportError as exc:
                    logger.warning(f"Could not load provider module {module_info.name}: {exc}")

    @classmethod
    def initialize_providers(cls):
        if cls._initialized:
            return

        cls._discover_provider_classes()
        cls._initialized = True

    @classmethod
    def is_initialized(cls) -> bool:
        return cls._initialized


def init_providers(app=None):
    ProviderRegistry.initialize_providers()

    if app:
        app.logger.info(f"Initialized {len(ProviderRegistry.get_all())} subtitle providers")

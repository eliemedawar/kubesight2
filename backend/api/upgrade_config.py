"""Upgrade automation configuration (Docker Desktop excluded)."""

from __future__ import annotations

from .runtime_config import env_flag


def auto_upgrade_enabled() -> bool:
    """Whether KubeSight itself runs kubeadm / supported-CLI cluster upgrades.

    Off unless ``KUBESIGHT_AUTO_UPGRADE`` is truthy (documented default
    ``false``). Without it, an upgrade request returns the manual plan with the
    provider's instructions instead of driving ``kubeadm upgrade`` on the
    nodes — an operator must opt in to KubeSight executing upgrades.
    """
    return env_flag("KUBESIGHT_AUTO_UPGRADE", False)

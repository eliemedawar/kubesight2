"""Upgrade automation configuration (Docker Desktop excluded)."""

from __future__ import annotations


def auto_upgrade_enabled() -> bool:
    """KubeSight runs automated upgrades for kubeadm and supported CLI providers."""
    return True

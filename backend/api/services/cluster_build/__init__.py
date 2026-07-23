"""Cluster Builder engine.

``profiles``     — BuildProfile CRUD + resolution (repo mode → concrete URLs).
``os_adapters``  — per-distro node preparation scripts (debian | rhel).
``cni``          — CNI plugin descriptors (calico | flannel | cilium).
``scrub``        — secret scrubbing applied to every persisted log line.
``kubeadm``      — config rendering + init-output parsing (join cmd, cert key).
``preflight``    — vCenter placement checks + per-node SSH readiness checks.
``executor``     — the restart-safe phase state machine.
``onboard``      — admin.conf → cluster_store → visible KubeSight cluster.
``service``      — build CRUD/serialization + wizard options.
"""

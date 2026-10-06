"""KubeSight creates the VMs itself, with OpenTofu, before it builds the cluster.

Modules:
  templates      built-in + saved cluster shapes, and the rules every shape obeys
  ip_pool        network ranges and address reservations
  inventory      vCenter placement + VM-template inventory (pyvmomi)
  tofu_config    main.tf.json for a build's VMs
  tofu_runner    the ``tofu`` process (real) and a simulated one (tests, demos)
  state_store    OpenTofu state + lock, served through KubeSight's HTTP backend
  jobs           plan / apply / destroy as restart-safe background jobs
"""

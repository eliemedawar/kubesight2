# KubeSight build agent

Runs builds on a machine KubeSight cannot reach into — a Mac that builds iOS, a
VM with a licensed toolchain, a host behind a firewall.

The agent **pulls**: it connects out to KubeSight, says what it can do, and asks
for work. KubeSight never opens a connection to it, so the machine needs no
inbound firewall rule, no public address and no VPN into the cluster.

**Why macOS needs an agent at all:** Apple's toolchain only runs on Apple
hardware. An `.ipa` cannot be produced in a Kubernetes pod under any
configuration, so an agent is not a convenience there — it is the only route.

## Requirements

- Python 3.6 or newer — the agent uses only the standard library, so there is
  nothing to `pip install`. 3.6 is the floor on purpose: it is what RHEL and
  CentOS 7 ship, and those are exactly the long-lived build hosts an agent
  exists to reach. If `python3 --version` is older than that, run it with an
  explicit interpreter (`python3.8 kubesight-agent.py …`).
- Whatever the builds need: git, a JDK, Gradle, Xcode. The agent reports what it
  finds on every heartbeat, so stages are only routed to machines that have the
  tools they ask for.

## Install

1. In KubeSight: **Service Catalog → Runners → Add agent**. Name it after the
   machine, choose Linux or macOS, save. The token is shown **once** — copy it.
2. Copy `kubesight-agent.py` onto the machine.
3. Run it:

```bash
python3 kubesight-agent.py --url https://kubesight.example.com --token <TOKEN>
```

By default, build data is written under `/data/kubesight-agent`, not under
root's home directory. Create it once for the account that runs the agent:

```bash
sudo install -d -o builder -g builder /data/kubesight-agent
```

Within a few seconds the runner shows **online** in KubeSight, listing the
capabilities it detected. Add `--insecure` if KubeSight uses a self-signed
certificate.

## Keep it running

**Linux (systemd)** — `/etc/systemd/system/kubesight-agent.service`:

```ini
[Unit]
Description=KubeSight build agent
After=network-online.target

[Service]
User=builder
Environment=KUBESIGHT_AGENT_TOKEN=<TOKEN>
ExecStart=/usr/bin/python3 /opt/kubesight/kubesight-agent.py --url https://kubesight.example.com
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now kubesight-agent
```

**macOS (launchd)** — `~/Library/LaunchAgents/com.kubesight.agent.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.kubesight.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/builder/kubesight/kubesight-agent.py</string>
    <string>--url</string><string>https://kubesight.example.com</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>KUBESIGHT_AGENT_TOKEN</key><string>TOKEN</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.kubesight.agent.plist
```

Use a **LaunchAgent** (not a daemon) for iOS work: signing needs the user's
login keychain, which a system daemon cannot reach. The user must be logged in.

## Sending a stage to an agent

In the pipeline editor, set the stage's **Runner** to *Linux agent* or *macOS
agent*, and list what it needs under **Required capabilities** (`xcode`,
`java`, `fastlane`). A stage only goes to a machine advertising all of them.

Container image stages stay on the Kubernetes runner — they mean "build with
BuildKit", which is the cluster's job. An agent says so rather than pretending.

## What it does on the machine

- One directory per build under the build directory, so a build's stages share
  a workspace exactly as they do in a pod. That path can be set from KubeSight
  (Runners → the agent → **Build directory**) and the agent applies it on its
  next heartbeat, reporting back if it cannot write there. A `--workspace` given
  on the command line always wins — the person at the machine knows its disks.
  With neither, it is `/data/kubesight-agent`. You may also set
  `KUBESIGHT_AGENT_WORKSPACE` locally.
- A workspace is removed automatically after KubeSight confirms the entire
  build is finished. A 24-hour stale sweep removes marked directories left by
  a crash or a lost response. Set `--workspace-retention-hours` (or
  `KUBESIGHT_AGENT_WORKSPACE_RETENTION_HOURS`) to change that recovery window;
  `0` disables only the stale sweep. The shared `.cache` is preserved.
- Commands run with `/bin/sh -e`, in the checkout, with the stage's environment
  and secrets injected. `KUBESIGHT_WORKSPACE` and `KUBESIGHT_SOURCE` name this
  build's directories — use those rather than a literal `/workspace`, which is
  the Kubernetes runner's path and does not exist here. Secrets are held in memory for the length of the task
  and never written to disk.
- Git credentials travel as `GIT_CONFIG_*` environment variables, never in
  argv, so they cannot be read from the process list on a shared machine.
- Declared artifacts are uploaded when the stage succeeds.

## Stages in containers (Linux)

On Linux, a stage that declares a container image runs **inside that image**
when the machine has `docker` or `podman`. The machine then needs no JDK, no
Gradle and no Node of its own, and the stage produces the same build the
cluster would: same image, same tool versions.

    [agent] running in gradle:9.1.0-jdk17 (docker)

What the container gets:

- the build's workspace at `/workspace`, its checkout at `/workspace/source`
  — **the same paths the Kubernetes runner uses**, so one pipeline's commands
  are correct on either runner;
- a cache directory at `/cache`, shared by every build on this machine, with
  Maven, Gradle, npm, yarn, pip and Go pointed into it, so a containerised
  stage is not slower than a host one;
- the stage's environment and secrets, passed by name so their values never
  appear in `docker run`'s arguments, where any user on the machine could read
  them from the process list;
- the stage's host aliases as `--add-host`, which an agent cannot otherwise
  honour (it cannot write `/etc/hosts`);
- `--user` set to the agent's own uid, so files come back owned by the agent
  and not by root; on SELinux hosts the mounts are `:z` labelled.

A stage can decide for itself with `KUBESIGHT_CONTAINER` in its environment:

| value | behaviour |
| --- | --- |
| `auto` (default) | in a container when this machine can, otherwise on the machine |
| `always` | refuse to run the stage outside a container |
| `never` | always run on the machine itself |

`--no-container` turns it off for the whole machine, and `--runtime docker`
(or `podman`) pins which runtime is used, so a machine that gets the other one
installed later does not quietly change how its builds run. Without it, docker
is preferred and podman is the fallback. An agent that can
containerise reports a `container` capability, so a stage that must be
containerised can require it as a runner label rather than discovering the
machine's toolchain the hard way.

**Where it does not apply:** macOS and Windows agents never containerise — a
container there is a Linux VM, which is exactly what an iOS build cannot use.
Checkout always runs on the machine (it needs only git). Container *image*
stages are still built by BuildKit in the cluster, never by an agent.

When there is no runtime and a stage declares an image, the agent says so in
the log and runs with the machine's own tools — the build that ran is not the
build the image would have produced, so it is never silent about it.

## How quickly it picks work up

- Idle, it asks for work every couple of seconds. KubeSight sets that interval
  and hands it out on every heartbeat (`CI_AGENT_POLL_SECONDS` on the backend),
  so the whole fleet is tuned in one place — a `--poll` given on the command
  line pins this machine's own value instead.
- Between a build's stages there is nothing to wait for: posting a stage's exit
  code is what queues the next stage, so the claim the agent makes immediately
  afterwards already has it. For a few seconds after finishing a task it asks on
  a much faster beat for exactly that reason.

## Security

- The token authenticates the machine. Treat it as a credential: it can claim
  any build routed to that runner, and those payloads carry that build's
  secrets. Rotate it from the Runners dialog if a machine is lost — the old
  token stops working immediately.
- The agent runs build commands from your repositories with the privileges of
  the user it runs as. Give it its own unprivileged account, not yours.
- With container mode, those commands run as the agent's uid inside the image
  rather than on the machine — narrower, but the container still has the
  workspace and the stage's secrets. Rootless `podman` is the stronger choice
  here: with Docker, membership of the `docker` group is equivalent to root on
  that host, so a machine where builds must not reach root should use podman.
- Containers this agent starts are labelled `kubesight.agent=1` and removed
  when the stage ends; leftovers from a killed agent are pruned at startup.
  Nothing else on the machine is touched.
- All traffic is outbound HTTPS to KubeSight.

## Taking a machine out of service

Disable the runner in KubeSight. The agent learns on its next heartbeat and
stops claiming, but finishes what it already started. Stop the service when the
build it was running has completed.

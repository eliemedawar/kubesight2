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

- Python 3.8+ — ships with macOS and every current Linux, and the agent uses
  only the standard library. There is nothing to `pip install`.
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
  With neither, it is `~/kubesight-agent`.
- Commands run with `/bin/sh -e`, in the checkout, with the stage's environment
  and secrets injected. Secrets are held in memory for the length of the task
  and never written to disk.
- Git credentials travel as `GIT_CONFIG_*` environment variables, never in
  argv, so they cannot be read from the process list on a shared machine.
- Declared artifacts are uploaded when the stage succeeds.

**It does not containerise anything.** A stage's "container image" is ignored
here: the point of an agent is to use the machine as it is.

## Security

- The token authenticates the machine. Treat it as a credential: it can claim
  any build routed to that runner, and those payloads carry that build's
  secrets. Rotate it from the Runners dialog if a machine is lost — the old
  token stops working immediately.
- The agent runs build commands from your repositories with the privileges of
  the user it runs as. Give it its own unprivileged account, not yours.
- All traffic is outbound HTTPS to KubeSight.

## Taking a machine out of service

Disable the runner in KubeSight. The agent learns on its next heartbeat and
stops claiming, but finishes what it already started. Stop the service when the
build it was running has completed.

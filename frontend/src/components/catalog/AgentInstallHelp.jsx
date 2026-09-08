import { useState } from "react";

/**
 * How to actually install the agent, at the moment somebody has to go and do it.
 *
 * Written for the machine, not for KubeSight: the token belongs in a service
 * manager's environment, not in a shell history, and the two managers that keep
 * a process alive differ enough that showing both at once would be noise. So the
 * platform the operator picked decides what is shown.
 */
function CopyBlock({ label, value, hint }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard is blocked in some browsers over plain HTTP; the text is on
      // screen and selectable either way.
      setCopied(false);
    }
  };

  return (
    <div className="sg-ci-help-block">
      <div className="sg-ci-help-block-head">
        <span>{label}</span>
        <button type="button" className="btn-outline btn-compact" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre>{value}</pre>
      {hint && <p className="field-hint">{hint}</p>}
    </div>
  );
}

export default function AgentInstallHelp({ runnerType, url, token, workspaceRoot }) {
  const isMac = runnerType === "agent_macos";
  const base = url || "https://kubesight.example.com";
  // A placeholder rather than a blank when no token has been issued yet: this
  // panel is also read before registering, to see what the work will involve.
  const value = token || "<TOKEN>";
  const workspaceArg = workspaceRoot ? ` --workspace ${workspaceRoot}` : "";
  const home = isMac ? "/Users/builder" : "/opt/kubesight";

  const tryItOut = `python3 kubesight-agent.py --url ${base} --token ${value}${workspaceArg}`;

  const unit = isMac
    ? `<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.kubesight.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>${home}/kubesight-agent.py</string>
    <string>--url</string><string>${base}</string>${
        workspaceRoot ? `\n    <string>--workspace</string><string>${workspaceRoot}</string>` : ""
      }
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>KUBESIGHT_AGENT_TOKEN</key><string>${value}</string>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>`
    : `[Unit]
Description=KubeSight build agent
After=network-online.target

[Service]
User=builder
Environment=KUBESIGHT_AGENT_TOKEN=${value}
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/usr/bin/python3 ${home}/kubesight-agent.py --url ${base}${workspaceArg}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target`;

  return (
    <div className="sg-ci-help">
      <ol className="sg-ci-help-steps">
        <li>
          <strong>Copy the agent onto the machine.</strong> It is{" "}
          <code>agent/kubesight-agent.py</code> in the KubeSight repository — one file,
          no dependencies, and it runs on Python 3.6+, which{" "}
          {isMac ? "macOS" : "even a RHEL 7 build host"} already has. Put it at{" "}
          <code>{home}/kubesight-agent.py</code>.
        </li>

        <li>
          <strong>Try it once, in a shell.</strong> Confirms the token and the network
          before a service manager hides the output.
          <CopyBlock
            label={isMac ? "Terminal on the Mac" : "Shell on the host"}
            value={tryItOut}
            hint="The runner turns online here within a few seconds. Ctrl-C to stop."
          />
        </li>

        <li>
          <strong>Keep it running.</strong> The token goes in the service definition,
          not on the command line, so it stays out of shell history and process lists.
          <CopyBlock
            label={
              isMac
                ? "~/Library/LaunchAgents/com.kubesight.agent.plist"
                : "/etc/systemd/system/kubesight-agent.service"
            }
            value={unit}
          />
          <CopyBlock
            label="Then"
            value={
              isMac
                ? "launchctl load ~/Library/LaunchAgents/com.kubesight.agent.plist"
                : "sudo systemctl daemon-reload && sudo systemctl enable --now kubesight-agent"
            }
          />
        </li>

        <li>
          <strong>Check what it advertises.</strong> The capability chips on this
          runner list the tools the agent actually found. If <code>java</code> or{" "}
          <code>gradle</code> is missing but works in your shell, the service manager's{" "}
          <code>PATH</code> is the cause — that is why <code>PATH</code> is set
          explicitly above; extend it to wherever your tools live.
        </li>
      </ol>

      {isMac && (
        <p className="banner-message info">
          Use a <strong>LaunchAgent</strong>, not a daemon, for iOS builds: signing
          reads the user's login keychain, which a system daemon cannot reach. The
          user has to stay logged in.
        </p>
      )}

      <p className="muted sg-ci-run-note">
        The token authenticates the machine and can claim any build routed to this
        runner, so treat it as a credential and give the agent its own unprivileged
        account. If a machine is lost, issue a new token here — the old one stops
        working immediately.
      </p>
    </div>
  );
}

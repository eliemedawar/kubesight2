/** The Blueprint — one drawing that follows a build from configure to done.
 *
 *  It replaces four unrelated pictures of the same cluster (the pick-time
 *  composer, the preflight verdict, the phase rail's node lanes, and the
 *  completed-build glyph). The geometry never changes; only the state layer
 *  does — empty, assigned, stamped with a preflight verdict, joining, joined.
 */

const STAMP_GLYPHS = { ok: "✓", warn: "!", bad: "✕", live: "●" };
const STAMP_LABELS = {
  ok: "passed",
  warn: "warning",
  bad: "failed",
  live: "working now",
};

const STATE_CAPTIONS = {
  outline: "outline",
  stamped: "stamped",
  live: "live",
  stopped: "stopped",
  built: "as built",
};

function Slot({ slot, role }) {
  const cls = [
    "sg-cb-slot",
    role === "loadbalancer" ? "is-lb" : "",
    role === "control_plane" ? "is-cp" : "",
    role === "worker" ? "is-w" : "",
    `is-${slot.state}`,
    slot.tie ? "is-tie" : "",
  ].filter(Boolean).join(" ");

  if (slot.state === "empty") {
    return <div className={cls}>assign</div>;
  }
  return (
    <div className={cls}>
      {slot.stamp ? (
        <span className={`sg-cb-stamp is-${slot.stamp}`}>
          <span className="sg-cb-sr">{STAMP_LABELS[slot.stamp]}</span>
          <span aria-hidden="true">{STAMP_GLYPHS[slot.stamp]}</span>
        </span>
      ) : null}
      <span className="sg-cb-slot-name">{slot.name}</span>
      {slot.sub ? <span className="sg-cb-slot-sub">{slot.sub}</span> : null}
    </div>
  );
}

/**
 * @param plan     from draftBlueprint / preflightBlueprint / buildBlueprint
 * @param note     {tone: 'good'|'warn'|'bad'|'plain', text} shown under the drawing
 * @param facts    [{label, value}] printed as a small ledger (add-ons, etcd, …)
 * @param footer   action area, usually the step's primary button
 */
export default function Blueprint({ plan, note, facts = [], footer, caption }) {
  if (!plan) return null;
  const { bus, tiers } = plan;
  const busState = bus?.state || "idle";
  return (
    <section className="sg-cb-bp" aria-label="Cluster blueprint">
      <header className="sg-cb-bp-head">
        <h3>Blueprint</h3>
        <span className="sg-cb-bp-state">{caption || STATE_CAPTIONS[plan.state] || plan.state}</span>
      </header>

      {bus?.address ? (
        <>
          <div className={`sg-cb-bus is-${busState}`}>
            <span className="sg-cb-bus-dot" />
            <span>
              {bus.label}{" "}
              <b className="sg-cb-mono">{bus.address}{bus.port ? `:${bus.port}` : ""}</b>
            </span>
            {busState === "up" ? <span className="sg-cb-bus-up">answering</span> : null}
            <span className="sg-cb-bus-wire" />
          </div>
          <div className="sg-cb-bp-drop" aria-hidden="true" />
        </>
      ) : null}

      {tiers.map((tier, index) => (
        <div className="sg-cb-tier" key={tier.role}>
          {index > 0 ? <div className="sg-cb-bp-drop" aria-hidden="true" /> : null}
          <div className="sg-cb-tier-head">
            <span>{tier.label}</span>
            <span className={`sg-cb-tier-n ${tier.target && tier.filled === tier.target ? "is-full" : ""}`}>
              {tier.target ? `${tier.filled} / ${tier.target}` : tier.filled || 0}
            </span>
          </div>
          <div className="sg-cb-slots">
            {tier.slots.map((slot) => (
              <Slot key={slot.key} slot={slot} role={tier.role} />
            ))}
          </div>
        </div>
      ))}

      {facts.length ? (
        <div className="sg-cb-bp-facts">
          {facts.map((fact) => (
            <div className="sg-cb-bp-fact" key={fact.label}>
              <span>{fact.label}</span>
              <b className="sg-cb-mono">{fact.value}</b>
            </div>
          ))}
        </div>
      ) : null}

      {note ? (
        <p className={`sg-cb-bp-note is-${note.tone || "plain"}`}>{note.text}</p>
      ) : null}

      {footer ? <div className="sg-cb-bp-foot">{footer}</div> : null}
    </section>
  );
}

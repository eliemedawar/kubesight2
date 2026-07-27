/** The phase rail, horizontal.
 *
 *  Vertically the ten phases cost ten rows and push the log below the fold.
 *  Read across, the same information looks like the pipeline it is, and leaves
 *  the screen for the two things someone actually watches while a cluster
 *  builds: the machines and the output.
 */

import { PHASE_LABELS, PHASE_SHORT } from "../../utils/clusterBuilder.js";

export default function PhaseRail({ timeline, onSelectPhase, activePhase }) {
  if (!timeline?.length) return null;
  return (
    <ol className="sg-cb-hrail" aria-label="Build phases">
      {timeline.map((cell, index) => {
        const interactive = Boolean(onSelectPhase) && cell.steps.length > 0;
        const label = PHASE_LABELS[cell.phase] || cell.phase;
        const content = (
          <>
            <span className="sg-cb-hknot">
              {cell.state === "done" ? "✓" : cell.state === "fail" ? "✕" : index + 1}
            </span>
            <span className="sg-cb-hcap">{PHASE_SHORT[cell.phase] || cell.phase}</span>
          </>
        );
        return (
          <li
            key={cell.phase}
            className={`sg-cb-hstep is-${cell.state} ${activePhase === cell.phase ? "is-open" : ""}`}
          >
            {interactive ? (
              <button type="button" onClick={() => onSelectPhase(cell)} title={label}>
                {content}
              </button>
            ) : (
              <span title={label}>{content}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

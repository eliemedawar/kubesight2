import { IconAlert, IconCheck, IconMinusCircle } from "./icons.jsx";

// Maps a backend status string to a Signal tone (sg-ico / status-pill modifier).
export function healthTone(status) {
  if (status === "ok") return "ok";
  if (status === "error") return "danger";
  return "muted";
}

const TONE_ICON = {
  ok: IconCheck,
  danger: IconAlert,
  warn: IconAlert,
  muted: IconMinusCircle,
};

// One row of the "Sync health" card: round tone icon, title + message, pill + timestamp.
export function HealthRow({ tone = "muted", title, message, right, time, tags }) {
  const Icon = TONE_ICON[tone] || IconMinusCircle;
  return (
    <div className="sg-zh-hrow">
      <span className={`sg-ico sg-ico--${tone}`}>
        <Icon />
      </span>
      <div className="sg-zh-hbody">
        <b>{title}</b>
        {message ? <span className="sg-zh-hmsg">{message}</span> : null}
        {tags ? <div className="sg-zh-tags">{tags}</div> : null}
      </div>
      {right || time ? (
        <div className="sg-zh-hstat">
          {right}
          {time ? <span className="sg-zh-htime">{time}</span> : null}
        </div>
      ) : null}
    </div>
  );
}

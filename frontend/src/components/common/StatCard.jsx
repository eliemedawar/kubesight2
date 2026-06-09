export default function StatCard({
  title,
  value,
  detail,
  tone = "default",
  icon,
  onClick,
  className = "",
  children,
  unavailable,
}) {
  const Tag = onClick ? "button" : "section";
  const toneClass = unavailable ? "tone-unknown" : `tone-${tone}`;

  return (
    <Tag
      type={onClick ? "button" : undefined}
      className={`card stat-card ${toneClass} ${onClick ? "stat-card-clickable" : ""} ${className}`.trim()}
      onClick={onClick}
    >
      <div className="stat-card-head">
        <div>
          <p className="eyebrow">{title}</p>
          <h3>{value}</h3>
        </div>
        {icon ? (
          <div className="stat-icon" aria-hidden="true">
            {icon}
          </div>
        ) : null}
      </div>
      {detail ? <p className="muted">{detail}</p> : null}
      {children}
    </Tag>
  );
}

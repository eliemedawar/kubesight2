import { useState } from "react";

/**
 * Password input with a show/hide toggle and a Caps Lock warning — the two
 * most common causes of "wrong password" get first-class affordances.
 */
export default function PasswordField({
  label,
  value,
  onChange,
  autoComplete = "current-password",
  autoFocus = false,
  required = true,
  disabled = false,
}) {
  const [visible, setVisible] = useState(false);
  const [capsOn, setCapsOn] = useState(false);

  const syncCaps = (event) => {
    setCapsOn(Boolean(event.getModifierState && event.getModifierState("CapsLock")));
  };

  return (
    <label className="sg-lg-field">
      {label}
      <span className="sg-lg-inputwrap">
        <input
          type={visible ? "text" : "password"}
          value={value}
          onChange={onChange}
          autoComplete={autoComplete}
          autoFocus={autoFocus}
          required={required}
          disabled={disabled}
          onKeyDown={syncCaps}
          onKeyUp={syncCaps}
          onFocus={syncCaps}
          onBlur={() => setCapsOn(false)}
        />
        <button
          type="button"
          className="sg-lg-eye"
          aria-pressed={visible}
          aria-label={visible ? "Hide password" : "Show password"}
          onClick={() => setVisible((v) => !v)}
        >
          {visible ? (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" />
              <circle cx="12" cy="12" r="3" />
              <path d="m4 20 16-16" />
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" />
              <circle cx="12" cy="12" r="3" />
            </svg>
          )}
        </button>
      </span>
      {capsOn ? (
        <span className="sg-lg-caps">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M12 4 4 13h5v7h6v-7h5L12 4Z" />
          </svg>
          Caps Lock is on
        </span>
      ) : null}
    </label>
  );
}

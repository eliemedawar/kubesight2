import { useEffect, useRef } from "react";

const LENGTH = 6;

/**
 * Segmented 6-digit one-time-code input: auto-advances as you type, backspace
 * walks left, paste fills every cell, and `onComplete` fires once when the
 * sixth digit lands (so the parent can auto-submit). Controlled via a single
 * string `value`; exposed to screen readers as one labelled group.
 */
export default function OtpInput({
  value,
  onChange,
  onComplete,
  disabled = false,
  autoFocus = false,
  label = "Verification code",
}) {
  const refs = useRef([]);
  const digits = Array.from({ length: LENGTH }, (_, i) => value[i] || "");

  useEffect(() => {
    if (autoFocus && refs.current[0]) {
      refs.current[0].focus();
    }
  }, [autoFocus]);

  const commit = (next) => {
    const clean = next.replace(/\D/g, "").slice(0, LENGTH);
    onChange(clean);
    if (clean.length === LENGTH && value.length < LENGTH && onComplete) {
      onComplete(clean);
    }
  };

  const handleInput = (index, raw) => {
    const ch = raw.replace(/\D/g, "").slice(-1);
    const chars = digits.slice();
    chars[index] = ch;
    commit(chars.join(""));
    if (ch && index < LENGTH - 1) {
      refs.current[index + 1]?.focus();
    }
  };

  const handleKeyDown = (index, event) => {
    if (event.key === "Backspace" && !digits[index] && index > 0) {
      const chars = digits.slice();
      chars[index - 1] = "";
      commit(chars.join(""));
      refs.current[index - 1]?.focus();
      event.preventDefault();
    } else if (event.key === "ArrowLeft" && index > 0) {
      refs.current[index - 1]?.focus();
    } else if (event.key === "ArrowRight" && index < LENGTH - 1) {
      refs.current[index + 1]?.focus();
    }
  };

  const handlePaste = (event) => {
    const text = (event.clipboardData?.getData("text") || "").replace(/\D/g, "").slice(0, LENGTH);
    if (!text) {
      return;
    }
    event.preventDefault();
    commit(text);
    refs.current[Math.min(text.length, LENGTH - 1)]?.focus();
  };

  return (
    <div className="sg-lg-otp" role="group" aria-label={label} onPaste={handlePaste}>
      {digits.map((digit, i) => (
        <input
          key={i}
          ref={(el) => {
            refs.current[i] = el;
          }}
          type="text"
          inputMode="numeric"
          autoComplete={i === 0 ? "one-time-code" : "off"}
          maxLength={2}
          className={digit ? "is-filled" : ""}
          aria-label={`Digit ${i + 1}`}
          value={digit}
          disabled={disabled}
          onChange={(e) => handleInput(i, e.target.value)}
          onKeyDown={(e) => handleKeyDown(i, e)}
        />
      ))}
    </div>
  );
}

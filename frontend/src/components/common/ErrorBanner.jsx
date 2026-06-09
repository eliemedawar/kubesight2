import { formatAccessError } from "../../utils/authz";

export default function ErrorBanner({
  message,
  className = "banner-message error",
  suppressAccessDenied = true,
}) {
  const text = formatAccessError(message, { suppressAccessDenied });
  if (!text) {
    return null;
  }
  return <p className={className}>{text}</p>;
}

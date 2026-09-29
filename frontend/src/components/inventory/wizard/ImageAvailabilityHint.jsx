import { useEffect, useState } from "react";
import { checkImage } from "../../../api/registriesApi.js";

const BADGES = {
  found: { label: "✓ Available in registry", className: "image-hint--ok" },
  not_found: { label: "✕ Not found in registry", className: "image-hint--error" },
  unreachable: { label: "! Registry unreachable", className: "image-hint--warn" },
};

/**
 * Live availability indicator for a container image. Debounces a call to the
 * linked-registry check endpoint. Renders nothing until there's something to
 * say — and stays silent for images with no matching linked registry
 * (``no_connection``), so it never nags about Docker Hub images. With a
 * ``clusterId`` whose cluster has linked registries, the image is checked in
 * those registries, as the deploy gate will.
 */
export default function ImageAvailabilityHint({ image, tag, clusterId }) {
  const ref = [image?.trim(), tag?.trim()].filter(Boolean).join(":");
  const [result, setResult] = useState(null);

  useEffect(() => {
    if (!image || !image.trim()) {
      setResult(null);
      return undefined;
    }
    let cancelled = false;
    const handle = setTimeout(async () => {
      try {
        const data = await checkImage(ref || image.trim(), clusterId || undefined);
        if (!cancelled) {
          setResult(data);
        }
      } catch {
        if (!cancelled) {
          setResult(null);
        }
      }
    }, 500);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [image, tag, ref, clusterId]);

  if (!result || result.status === "no_connection") {
    return null;
  }
  const badge = BADGES[result.status];
  if (!badge) {
    return null;
  }
  return (
    <p className={`image-hint ${badge.className}`} title={result.message}>
      {badge.label}
      {result.registry ? <span className="image-hint__registry"> · {result.registry}</span> : null}
    </p>
  );
}

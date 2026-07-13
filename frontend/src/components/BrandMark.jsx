import mark from "../assets/kubesight-mark.png";

/**
 * KubeSight brand mark — the cube-with-eye icon cropped from the brand logo,
 * background removed so it sits cleanly on light and dark surfaces.
 * The full logo lockup (mark + wordmark) lives at assets/kubesight-logo.png.
 */
export default function BrandMark({ className }) {
  return <img src={mark} alt="" className={className} draggable="false" />;
}

/**
 * The Markdown Hermes writes, parsed into plain blocks the assistant renders as
 * React elements — never HTML strings, so nothing in a reply (or in the pod
 * log it quotes) can inject markup.
 *
 * Blocks: heading, paragraph, code (fenced), list (ordered or not), quote,
 * table, rule. Inline: `code`, **bold**, *italic*, [text](https://…) links.
 */

const FENCE = /^\s*(```|~~~)\s*([\w+.-]*)\s*$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const ORDERED = /^\s*(\d+)[.)]\s+(.*)$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function splitRow(line) {
  let text = line.trim();
  if (text.startsWith("|")) text = text.slice(1);
  if (text.endsWith("|")) text = text.slice(0, -1);
  return text.split("|").map((cell) => cell.trim());
}

function isBlockStart(line, next) {
  return (
    FENCE.test(line) ||
    HEADING.test(line) ||
    BULLET.test(line) ||
    ORDERED.test(line) ||
    RULE.test(line) ||
    /^\s*>/.test(line) ||
    (line.includes("|") && next !== undefined && TABLE_DIVIDER.test(next))
  );
}

export function parseBlocks(source) {
  const lines = String(source ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const fence = line.match(FENCE);
    if (fence) {
      const body = [];
      i += 1;
      while (i < lines.length && !new RegExp(`^\\s*${fence[1]}\\s*$`).test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // closing fence (or end of text: an unclosed fence still renders)
      blocks.push({ type: "code", lang: fence[2] || "", text: body.join("\n") });
      continue;
    }
    const heading = line.match(HEADING);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2].trim() });
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      blocks.push({ type: "rule" });
      i += 1;
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_DIVIDER.test(lines[i + 1])) {
      const head = splitRow(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      blocks.push({ type: "table", head, rows });
      continue;
    }
    if (/^\s*>/.test(line)) {
      const body = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        body.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      blocks.push({ type: "quote", text: body.join(" ") });
      continue;
    }
    const bullet = BULLET.test(line);
    const ordered = line.match(ORDERED);
    if (bullet || ordered) {
      const pattern = bullet ? BULLET : ORDERED;
      const items = [];
      while (i < lines.length) {
        const current = lines[i];
        const match = current.match(pattern);
        if (match) {
          items.push(bullet ? match[1] : match[2]);
        } else if (current.trim() && /^\s{2,}\S/.test(current) && items.length) {
          // An indented continuation line belongs to the item above.
          items[items.length - 1] += ` ${current.trim()}`;
        } else {
          break;
        }
        i += 1;
      }
      blocks.push({ type: "list", ordered: Boolean(ordered), start: ordered ? Number(ordered[1]) : 1, items });
      continue;
    }
    const body = [line.trim()];
    i += 1;
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i], lines[i + 1])) {
      body.push(lines[i].trim());
      i += 1;
    }
    blocks.push({ type: "paragraph", text: body.join("\n") });
  }
  return blocks;
}

const INLINE = /(`+)([\s\S]*?)\1|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\s][^*]*)\*|\[([^\]]+)\]\(([^)\s]+)\)/g;

export function safeHref(href) {
  const value = String(href || "").trim();
  if (/^https?:\/\//i.test(value) || value.startsWith("#/")) return value;
  return null;
}

/** Inline spans: [{type: "text"|"code"|"strong"|"em"|"link", text, href?}] */
export function parseInline(text) {
  const source = String(text ?? "");
  const spans = [];
  let last = 0;
  for (const match of source.matchAll(INLINE)) {
    if (match.index > last) spans.push({ type: "text", text: source.slice(last, match.index) });
    if (match[1]) spans.push({ type: "code", text: match[2].trim() });
    else if (match[3] || match[4]) spans.push({ type: "strong", text: match[3] || match[4] });
    else if (match[5]) spans.push({ type: "em", text: match[5] });
    else {
      const href = safeHref(match[7]);
      spans.push(href ? { type: "link", text: match[6], href } : { type: "text", text: match[0] });
    }
    last = match.index + match[0].length;
  }
  if (last < source.length) spans.push({ type: "text", text: source.slice(last) });
  return spans;
}

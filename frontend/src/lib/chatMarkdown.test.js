import { describe, expect, it } from "vitest";
import { parseBlocks, parseInline, safeHref } from "./chatMarkdown.js";

describe("parseBlocks", () => {
  it("reads headings, paragraphs, lists and fenced code", () => {
    const blocks = parseBlocks(
      "## Pods\nTwo are failing:\n\n- `api-1` CrashLoopBackOff\n- `api-2` OOMKilled\n\n```yaml\nreplicas: 2\n```"
    );
    expect(blocks.map((b) => b.type)).toEqual(["heading", "paragraph", "list", "code"]);
    expect(blocks[2].items).toEqual(["`api-1` CrashLoopBackOff", "`api-2` OOMKilled"]);
    expect(blocks[3]).toEqual({ type: "code", lang: "yaml", text: "replicas: 2" });
  });

  it("keeps a fenced block's content verbatim, Markdown included", () => {
    const [block] = parseBlocks("```\n# not a heading\n- not a list\n```");
    expect(block.text).toBe("# not a heading\n- not a list");
  });

  it("renders an unclosed fence instead of dropping it", () => {
    expect(parseBlocks("```\nlog line")).toEqual([{ type: "code", lang: "", text: "log line" }]);
  });

  it("reads a table", () => {
    const [table] = parseBlocks("| Pod | Status |\n|---|---|\n| a | Running |\n| b | Failed |");
    expect(table).toEqual({
      type: "table",
      head: ["Pod", "Status"],
      rows: [["a", "Running"], ["b", "Failed"]],
    });
  });

  it("numbers an ordered list from where it starts", () => {
    const [list] = parseBlocks("3. three\n4. four");
    expect(list).toMatchObject({ type: "list", ordered: true, start: 3, items: ["three", "four"] });
  });
});

describe("parseInline", () => {
  it("splits code, bold, italic and links", () => {
    expect(parseInline("Run `kubectl get pods` **now**, *carefully*: [docs](https://k8s.io)")).toEqual([
      { type: "text", text: "Run " },
      { type: "code", text: "kubectl get pods" },
      { type: "text", text: " " },
      { type: "strong", text: "now" },
      { type: "text", text: ", " },
      { type: "em", text: "carefully" },
      { type: "text", text: ": " },
      { type: "link", text: "docs", href: "https://k8s.io" },
    ]);
  });

  it("leaves a link with an unsafe scheme as plain text", () => {
    expect(parseInline("[x](javascript:alert(1))")[0].type).toBe("text");
    expect(safeHref("data:text/html,hi")).toBeNull();
    expect(safeHref("#/clusters")).toBe("#/clusters");
  });
});

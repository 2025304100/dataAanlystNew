import { useMemo } from "react";
import { theme } from "antd";

/**
 * 轻量 Markdown 渲染（零依赖，不用 dangerouslySetInnerHTML）。
 *
 * 背景：AI 回答常含代码块 / 列表 / 粗体 / 行内代码，此前整段按 pre-wrap 纯文本展示，
 * 公式与列表可读性差（评估报告 L4）。这里只做四类语法，超出的一律按纯文本保留换行：
 * - ``` 代码块（可带语言名，仅作展示不执行）
 * - - / * 无序列表、1. 有序列表
 * - **粗体**、`行内代码`
 *
 *  deliberately 不支持标题/表格/链接：回答以说明性文本为主，标题用加粗已够；
 * 需要完整语法时应评估引入 react-markdown + remark-gfm（新依赖需评审）。
 */
export default function MarkdownText({ content }: { content: string }) {
  const { token } = theme.useToken();

  const blocks = useMemo(() => splitBlocks(content), [content]);

  if (!content) return null;

  return (
    <div style={{ fontSize: 14, lineHeight: 1.6 }}>
      {blocks.map((block, idx) => {
        if (block.type === "code") {
          return (
            <pre
              key={idx}
              style={{
                margin: "8px 0",
                padding: "8px 10px",
                background: token.colorFillTertiary,
                borderRadius: token.borderRadius,
                overflowX: "auto",
                fontSize: 12,
                fontFamily: "var(--font-mono, monospace)",
              }}
            >
              <code>{block.lines.join("\n")}</code>
            </pre>
          );
        }
        if (block.type === "ul") {
          return (
            <ul key={idx} style={{ margin: "4px 0", paddingLeft: 20 }}>
              {block.lines.map((line, li) => (
                <li key={li} style={{ marginBottom: 2 }}>{renderInline(line, `${idx}-${li}`, token.colorError)}</li>
              ))}
            </ul>
          );
        }
        if (block.type === "ol") {
          return (
            <ol key={idx} style={{ margin: "4px 0", paddingLeft: 20 }}>
              {block.lines.map((line, li) => (
                <li key={li} style={{ marginBottom: 2 }}>{renderInline(line, `${idx}-${li}`, token.colorError)}</li>
              ))}
            </ol>
          );
        }
        return (
          <p key={idx} style={{ margin: "4px 0", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
            {block.lines.map((line, li) => (
              <span key={li}>
                {renderInline(line, `${idx}-${li}`, token.colorError)}
                {li < block.lines.length - 1 ? <br /> : null}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}

type Block =
  | { type: "code"; lines: string[] }
  | { type: "ul"; lines: string[] }
  | { type: "ol"; lines: string[] }
  | { type: "p"; lines: string[] };

/** 把文本切成 代码块 / 列表 / 段落 三类块。 */
export function splitBlocks(content: string): Block[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let para: string[] = [];
  let list: { ordered: boolean; lines: string[] } | null = null;
  let inCode = false;
  let codeLines: string[] = [];

  const flushPara = () => {
    if (para.length) {
      blocks.push({ type: "p", lines: para });
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      blocks.push({ type: list.ordered ? "ol" : "ul", lines: list.lines });
      list = null;
    }
  };

  for (const rawLine of lines) {
    const line = rawLine;
    const fence = line.trim().match(/^```(\w*)\s*$/);
    if (fence) {
      if (inCode) {
        blocks.push({ type: "code", lines: codeLines });
        codeLines = [];
        inCode = false;
      } else {
        flushPara();
        flushList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }
    const ulMatch = line.match(/^\s*[-*]\s+(.*)$/);
    const olMatch = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ulMatch || olMatch) {
      flushPara();
      const ordered = Boolean(olMatch);
      const text = (ulMatch ?? olMatch)![1];
      if (list && list.ordered !== ordered) flushList();
      if (!list) list = { ordered, lines: [] };
      list.lines.push(text);
      continue;
    }
    if (line.trim() === "") {
      flushPara();
      flushList();
      continue;
    }
    flushList();
    para.push(line);
  }
  if (inCode && codeLines.length) blocks.push({ type: "code", lines: codeLines });
  flushPara();
  flushList();
  return blocks;
}

/** 行内 **粗体** 与 `行内代码`。 */
function renderInline(text: string, keyPrefix: string, codeColor: string): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  const regex = /(\*\*[^*\n]+\*\*|`[^`\n]+`)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const tok = match[0];
    if (tok.startsWith("**")) {
      parts.push(<strong key={`${keyPrefix}-b${i}`}>{tok.slice(2, -2)}</strong>);
    } else {
      parts.push(
        <code
          key={`${keyPrefix}-c${i}`}
          style={{
            margin: "0 2px",
            padding: "1px 5px",
            borderRadius: 4,
            background: "rgba(0,0,0,0.06)",
            color: codeColor,
            fontFamily: "var(--font-mono, monospace)",
            fontSize: "0.92em",
          }}
        >
          {tok.slice(1, -1)}
        </code>,
      );
    }
    last = match.index + tok.length;
    i += 1;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

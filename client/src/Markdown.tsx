/** 极简 Markdown 渲染：够用于 Wiki 页面（标题/列表/表格/引用/行内代码/链接/[[wiki 链接]]）。 */
import type { ReactNode } from "react";

function inline(text: string, key: string, onWikiLink?: (slug: string, label: string) => void): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*)|(`[^`]+`)|(\[\[[^\]|]+\|?[^\]]*\]\])|(\[[^[\]]+\]\([^)]+\))/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  let index = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) nodes.push(text.slice(cursor, match.index));
    const token = match[0];
    const id = `${key}-${index++}`;
    if (token.startsWith("**")) {
      nodes.push(<strong key={id}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("`")) {
      nodes.push(<code key={id}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("[[")) {
      const body = token.slice(2, -2);
      const [slug, label] = body.split("|");
      nodes.push(
        <a
          key={id}
          href="#"
          onClick={(event) => {
            event.preventDefault();
            onWikiLink?.(slug.trim(), (label ?? slug).trim());
          }}
        >
          {label?.trim() || slug}
        </a>,
      );
    } else {
      const linkMatch = /\[([^[\]]+)\]\(([^)]+)\)/.exec(token);
      if (linkMatch) nodes.push(<a key={id} href={linkMatch[2]} target="_blank" rel="noreferrer">{linkMatch[1]}</a>);
    }
    cursor = match.index + token.length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

export function Markdown({ source, onWikiLink }: { source: string; onWikiLink?: (slug: string, label: string) => void }) {
  const lines = (source ?? "").split("\n");
  const blocks: ReactNode[] = [];
  let list: ReactNode[] = [];
  let table: string[][] | null = null;
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const text = paragraph.join(" ");
    blocks.push(<p key={`p-${blocks.length}`}>{inline(text, `p${blocks.length}`, onWikiLink)}</p>);
    paragraph = [];
  };
  const flushList = () => {
    if (!list.length) return;
    blocks.push(<ul key={`u-${blocks.length}`}>{list}</ul>);
    list = [];
  };
  const flushTable = () => {
    if (!table) return;
    const [head, ...body] = table.filter((row) => !row.join("|").includes("---"));
    blocks.push(
      <table key={`t-${blocks.length}`}>
        <thead>
          <tr>{head.map((cell, i) => <th key={i}>{inline(cell, `th${i}`, onWikiLink)}</th>)}</tr>
        </thead>
        <tbody>
          {body.map((row, r) => (
            <tr key={r}>{row.map((cell, c) => <td key={c}>{inline(cell, `td${r}${c}`, onWikiLink)}</td>)}</tr>
          ))}
        </tbody>
      </table>,
    );
    table = null;
  };

  lines.forEach((raw, lineIndex) => {
    const line = raw.replace(/\s+$/, "");
    const cells = (text: string) =>
      text
        .split("|")
        .map((cell) => cell.trim())
        .filter((cell, i, arr) => !(i === 0 && cell === "") && !(i === arr.length - 1 && cell === ""));

    if (/^#{1,4}\s/.test(line)) {
      flushParagraph();
      flushList();
      flushTable();
      const level = (line.match(/^#+/) as RegExpMatchArray)[0].length;
      const text = line.replace(/^#+\s/, "");
      const Tag = (`h${Math.min(level + 1, 5)}`) as "h2" | "h3" | "h4" | "h5";
      blocks.push(<Tag key={`h-${lineIndex}`}>{inline(text, `h${lineIndex}`, onWikiLink)}</Tag>);
    } else if (/^\s*[-*]\s+/.test(line)) {
      flushParagraph();
      flushTable();
      list.push(<li key={`l-${lineIndex}`}>{inline(line.replace(/^\s*[-*]\s+/, ""), `l${lineIndex}`, onWikiLink)}</li>);
    } else if (line.startsWith("|") && line.endsWith("|")) {
      flushParagraph();
      flushList();
      table = table ? [...table, cells(line)] : [cells(line)];
    } else if (line.startsWith("> ")) {
      flushParagraph();
      flushList();
      flushTable();
      blocks.push(<blockquote key={`q-${lineIndex}`}>{inline(line.slice(2), `q${lineIndex}`, onWikiLink)}</blockquote>);
    } else if (line.trim() === "") {
      flushParagraph();
      flushList();
      flushTable();
    } else {
      paragraph.push(line);
    }
  });
  flushParagraph();
  flushList();
  flushTable();

  return <div className="md">{blocks}</div>;
}

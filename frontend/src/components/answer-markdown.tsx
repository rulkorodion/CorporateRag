import type { Link, Parent, Root, Text } from "mdast";
import type { Plugin } from "unified";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function citationLinks(idPrefix: string): Plugin<[], Root> {
  return () => (tree) => {
    const walk = (parent: Parent) => {
      parent.children = parent.children.flatMap((child) => {
        if (child.type !== "text") {
          if ("children" in child) walk(child as Parent);
          return child;
        }
        const value = (child as Text).value;
        const pattern = /\[(\d+)]/g;
        const nodes: (Text | Link)[] = [];
        let cursor = 0;
        for (const match of value.matchAll(pattern)) {
          const index = match.index ?? 0;
          if (index > cursor) nodes.push({ type: "text", value: value.slice(cursor, index) });
          nodes.push({
            type: "link",
            url: `#${idPrefix}-source-${match[1]}`,
            children: [{ type: "text", value: match[0] }],
          });
          cursor = index + match[0].length;
        }
        if (!nodes.length) return child;
        if (cursor < value.length) nodes.push({ type: "text", value: value.slice(cursor) });
        return nodes;
      });
    };
    walk(tree);
  };
}

export function AnswerMarkdown({ answer, idPrefix, onCitationClick }: { answer: string; idPrefix: string; onCitationClick?: (sourceNumber: number) => void }) {
  return (
    <div className="answer-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, citationLinks(idPrefix)]}
        components={{
          a: ({ href, children }) => {
            const external = /^https?:\/\//i.test(href ?? "");
            return (
              <a
              className="font-medium text-[#4f7c9c] no-underline hover:underline"
              href={href}
              target={external ? "_blank" : undefined}
              rel={external ? "noopener noreferrer" : undefined}
              onClick={(event) => {
                const match = href?.match(/-source-(\d+)$/);
                if (match && onCitationClick) {
                  event.preventDefault();
                  onCitationClick(Number(match[1]));
                }
              }}
            >
              {children}
            </a>
            );
          },
        }}
      >
        {answer}
      </ReactMarkdown>
    </div>
  );
}

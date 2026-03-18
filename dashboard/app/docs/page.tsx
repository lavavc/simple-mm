import fs from 'fs';
import path from 'path';
import matter from 'gray-matter';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

const DOCS_DIR = path.join(process.cwd(), 'docs');

export default function DocsIndex() {
  const raw = fs.readFileSync(path.join(DOCS_DIR, 'index.md'), 'utf-8');
  const { content } = matter(raw);

  return <DocContent content={content} />;
}

export function DocContent({ content }: { content: string }) {
  return (
    <article className="prose prose-invert prose-sm max-w-none
      prose-headings:font-mono prose-headings:tracking-tight prose-headings:text-white
      prose-h1:text-xl prose-h1:mb-6 prose-h1:pb-3 prose-h1:border-b prose-h1:border-white/10
      prose-h2:text-base prose-h2:text-white/80 prose-h2:mt-10 prose-h2:mb-4
      prose-h3:text-sm prose-h3:text-white/70 prose-h3:mt-6
      prose-p:text-white/55 prose-p:leading-relaxed
      prose-a:text-emerald-400 prose-a:no-underline hover:prose-a:underline
      prose-strong:text-white/80
      prose-code:text-emerald-300 prose-code:bg-white/[0.06] prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:text-[0.8em] prose-code:font-mono prose-code:before:content-none prose-code:after:content-none
      prose-pre:bg-[#0c0f14] prose-pre:border prose-pre:border-white/[0.06] prose-pre:rounded-sm
      prose-table:text-xs prose-table:font-mono
      prose-th:text-white/60 prose-th:font-semibold prose-th:border-b prose-th:border-white/10
      prose-td:text-white/45 prose-td:border-b prose-td:border-white/[0.05]
      prose-li:text-white/55 prose-li:marker:text-white/25
      prose-hr:border-white/10
      prose-blockquote:border-l-white/20 prose-blockquote:text-white/40">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </article>
  );
}

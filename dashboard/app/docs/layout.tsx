import fs from 'fs';
import path from 'path';
import matter from 'gray-matter';
import { DocsSidebar, DocEntry, DocSection } from './DocsSidebar';

const DOCS_DIR = path.join(process.cwd(), 'docs');

function getDocTree(): { topLevel: DocEntry[]; sections: DocSection[] } {
  const topLevelRaw: (DocEntry & { order: number })[] = [];
  const sections: (DocSection & { order: number })[] = [];

  for (const f of fs.readdirSync(DOCS_DIR)) {
    const fullPath = path.join(DOCS_DIR, f);
    const stat = fs.statSync(fullPath);

    if (stat.isFile() && f.endsWith('.md') && f !== 'index.md' && !f.startsWith('_')) {
      const { data } = matter(fs.readFileSync(fullPath, 'utf-8'));
      topLevelRaw.push({
        slug: f.replace(/\.md$/, ''),
        title: (data.title as string) || f.replace(/\.md$/, '').replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
        order: (data.order as number) ?? 999,
      });
    } else if (stat.isDirectory() && f !== 'archive') {
      const metaPath = path.join(fullPath, '_index.md');
      let sectionTitle = f.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
      let sectionOrder = 999;

      if (fs.existsSync(metaPath)) {
        const { data } = matter(fs.readFileSync(metaPath, 'utf-8'));
        sectionTitle = (data.title as string) || sectionTitle;
        sectionOrder = (data.order as number) ?? 999;
      }

      const entries = fs
        .readdirSync(fullPath)
        .filter((sub) => sub.endsWith('.md') && !sub.startsWith('_'))
        .map((sub) => {
          const { data } = matter(fs.readFileSync(path.join(fullPath, sub), 'utf-8'));
          return {
            slug: `${f}/${sub.replace(/\.md$/, '')}`,
            title: (data.title as string) || sub.replace(/\.md$/, '').replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
            order: (data.order as number) ?? 999,
          };
        })
        .sort((a, b) => a.order - b.order)
        .map(({ slug, title }) => ({ slug, title }));

      sections.push({ title: sectionTitle, order: sectionOrder, entries });
    }
  }

  topLevelRaw.sort((a, b) => a.order - b.order);
  sections.sort((a, b) => a.order - b.order);

  return {
    topLevel: topLevelRaw.map(({ slug, title }) => ({ slug, title })),
    sections: sections.map(({ title, entries }) => ({ title, entries })),
  };
}

export default function DocsLayout({ children }: { children: React.ReactNode }) {
  const { topLevel, sections } = getDocTree();

  return (
    <div className="flex gap-10 max-w-5xl mx-auto py-10">
      <DocsSidebar topLevel={topLevel} sections={sections} />
      <div className="flex-1 min-w-0">{children}</div>
    </div>
  );
}
